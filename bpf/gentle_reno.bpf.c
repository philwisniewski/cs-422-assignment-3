// SPDX-License-Identifier: GPL-2.0
/*
 * Gentle Reno: TCP congestion control as an eBPF struct_ops program.
 * CS 422 Assignment 3, Option 1 (design from Assignment 2, Q3(b)).
 *
 *   loss (fast retransmit / RTO) : ssthresh = max(0.7 * cwnd, 2)           [loss]
 *                                  (the kernel then sets cwnd = ssthresh on
 *                                   recovery, or cwnd = 1 after an RTO)
 *   slow start                   : cwnd += 1 per ACKed packet (doubles / RTT)
 *       leave slow start (ssthresh = cwnd, no cut) at the end of a round if
 *         min RTT in the round > min_rtt + max(4 ms, min_rtt/8)           [RTT]
 *         or delivery rate grew < 25% for 3 consecutive rounds       [throughput]
 *   congestion avoidance         : cwnd += 0.5 packet per RTT
 *   undo (spurious loss)         : cwnd = max(cwnd, prior_cwnd)
 *
 * Loaded with:  bpftool struct_ops register gentle_reno.bpf.o
 * Selected with setsockopt(IPPROTO_TCP, TCP_CONGESTION, "gentle_reno").
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

char _license[] SEC("license") = "GPL";

/* ---- parameters ------------------------------------------------------- */
#define BETA_NUM            7       /* multiplicative decrease: 7/10 = 0.7  */
#define BETA_DEN            10
#define AI_DIVISOR          2       /* +1 packet per 2 RTTs = +0.5 / RTT    */
#define MIN_CWND            2
#define SS_DELAY_MIN_US     4000    /* D = max(4 ms, min_rtt / 8)            */
#define SS_DELAY_SHIFT      3
#define SS_BW_GROWTH_NUM    5       /* "grew" = at least 5/4 = 1.25x         */
#define SS_BW_GROWTH_DEN    4
#define SS_FLAT_ROUNDS      3

#define USEC_PER_SEC        1000000ULL
#define NO_RTT              0xffffffffU

/* ---- per-connection state (lives in icsk_ca_priv, max 104 bytes) ------ */
struct gentle_reno {
	__u32 min_rtt_us;        /* lowest RTT sample of the connection          */
	__u32 round_min_rtt_us;  /* lowest RTT sample in the current round       */
	__u32 end_seq;           /* the round ends when snd_una passes this      */
	__u32 round_delivered;   /* tp->delivered when the round started         */
	__u64 round_start_us;    /* tp->tcp_mstamp when the round started        */
	__u32 full_bw;           /* best delivery rate seen in slow start, pkt/s */
	__u8  flat_rounds;       /* rounds without >= 25% bandwidth growth       */
	__u8  ss_done;           /* slow-start exit rule has fired               */
};

/* ---- global counters, readable with `make stats` ------------------------ */
__u64 stat_init = 0;          /* connections that used gentle_reno         */
__u64 stat_loss_events = 0;   /* ssthresh() calls = loss/RTO episodes      */
__u64 stat_ss_exit_delay = 0; /* slow start left because RTT rose          */
__u64 stat_ss_exit_bw = 0;    /* slow start left because bandwidth plateau */
__u64 stat_undo = 0;          /* spurious-loss undos                        */

/* ---- kernel helpers available to BPF congestion control (kfuncs) ------- */
extern __u32 tcp_slow_start(struct tcp_sock *tp, __u32 acked) __ksym;
extern void tcp_cong_avoid_ai(struct tcp_sock *tp, __u32 w, __u32 acked) __ksym;

static __always_inline struct tcp_sock *tcp_sk(const struct sock *sk)
{
	return (struct tcp_sock *)sk;
}

static __always_inline struct gentle_reno *ca_of(const struct sock *sk)
{
	return (struct gentle_reno *)((struct inet_connection_sock *)sk)->icsk_ca_priv;
}

static __always_inline bool before(__u32 seq1, __u32 seq2)
{
	return (__s32)(seq1 - seq2) < 0;
}

static __always_inline bool in_slow_start(const struct tcp_sock *tp)
{
	return tp->snd_cwnd < tp->snd_ssthresh;
}

/* Same rule as the kernel's tcp_is_cwnd_limited(): only grow cwnd when the
 * sender is actually using it. */
static __always_inline bool cwnd_limited(const struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);

	if (in_slow_start(tp))
		return tp->snd_cwnd < 2 * tp->max_packets_out;
	return !!BPF_CORE_READ_BITFIELD(tp, is_cwnd_limited);
}

static __always_inline void start_round(struct tcp_sock *tp, struct gentle_reno *ca)
{
	ca->end_seq = tp->snd_nxt;
	ca->round_start_us = tp->tcp_mstamp;
	ca->round_delivered = tp->delivered;
	ca->round_min_rtt_us = NO_RTT;
}

SEC("struct_ops/gr_init")
void BPF_PROG(gr_init, struct sock *sk)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct gentle_reno *ca = ca_of(sk);

	/* Initial state: cwnd = 10 (kernel IW10), ssthresh = infinity (kernel
	 * default), min_rtt = infinity, full_bw = 0, flat_rounds = 0. */
	ca->min_rtt_us = NO_RTT;
	ca->full_bw = 0;
	ca->flat_rounds = 0;
	ca->ss_done = 0;
	start_round(tp, ca);
	__sync_fetch_and_add(&stat_init, 1);
}

/* LOSS: called once when the connection enters fast recovery (3 dup ACKs /
 * SACK / RACK) or loss state (RTO). The kernel sets cwnd from this value
 * (PRR during recovery) or to 1 after an RTO. */
SEC("struct_ops/gr_ssthresh")
__u32 BPF_PROG(gr_ssthresh, struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);
	__u32 next = tp->snd_cwnd * BETA_NUM / BETA_DEN;

	__sync_fetch_and_add(&stat_loss_events, 1);
	return next > MIN_CWND ? next : MIN_CWND;
}

/* GROWTH: called for ACKs that advance snd_una. */
SEC("struct_ops/gr_cong_avoid")
void BPF_PROG(gr_cong_avoid, struct sock *sk, __u32 ack, __u32 acked)
{
	struct tcp_sock *tp = tcp_sk(sk);

	if (!cwnd_limited(sk))
		return;

	if (in_slow_start(tp)) {
		acked = tcp_slow_start(tp, acked);   /* +1 per ACKed packet */
		if (!acked)
			return;
	}
	/* +1 packet every AI_DIVISOR * cwnd ACKed packets = +0.5 packet / RTT */
	tcp_cong_avoid_ai(tp, AI_DIVISOR * tp->snd_cwnd, acked);
}

/* RTT + THROUGHPUT: per-ACK RTT samples, evaluated once per round trip. */
SEC("struct_ops/gr_pkts_acked")
void BPF_PROG(gr_pkts_acked, struct sock *sk, const struct ack_sample *sample)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct gentle_reno *ca = ca_of(sk);
	__s32 rtt = sample->rtt_us;
	__u64 dt, bw;
	__u32 thresh;

	if (rtt > 0) {
		if ((__u32)rtt < ca->min_rtt_us)
			ca->min_rtt_us = rtt;
		if ((__u32)rtt < ca->round_min_rtt_us)
			ca->round_min_rtt_us = rtt;
	}

	if (before(tp->snd_una, ca->end_seq))
		return;                              /* round not finished yet */

	/* ---- a round trip ended ---- */
	if (in_slow_start(tp) && !ca->ss_done) {
		dt = tp->tcp_mstamp - ca->round_start_us;
		if (dt > 0) {
			/* throughput: packets delivered in this round / round time */
			bw = (__u64)(tp->delivered - ca->round_delivered) * USEC_PER_SEC / dt;
			if (bw * SS_BW_GROWTH_DEN >= (__u64)ca->full_bw * SS_BW_GROWTH_NUM) {
				ca->full_bw = bw > 0xffffffffULL ? 0xffffffffU : (__u32)bw;
				ca->flat_rounds = 0;
			} else if (ca->flat_rounds < 255) {
				ca->flat_rounds++;
			}
		}

		/* RTT rule: D = max(4 ms, min_rtt / 8) */
		if (ca->min_rtt_us != NO_RTT && ca->round_min_rtt_us != NO_RTT) {
			thresh = ca->min_rtt_us >> SS_DELAY_SHIFT;
			if (thresh < SS_DELAY_MIN_US)
				thresh = SS_DELAY_MIN_US;
			if (ca->round_min_rtt_us > ca->min_rtt_us + thresh) {
				tp->snd_ssthresh = tp->snd_cwnd;   /* leave slow start, no cut */
				ca->ss_done = 1;
				__sync_fetch_and_add(&stat_ss_exit_delay, 1);
			}
		}
		/* throughput rule: pipe is full */
		if (!ca->ss_done && ca->flat_rounds >= SS_FLAT_ROUNDS) {
			tp->snd_ssthresh = tp->snd_cwnd;
			ca->ss_done = 1;
			__sync_fetch_and_add(&stat_ss_exit_bw, 1);
		}
	}
	start_round(tp, ca);
}

/* After an RTO the kernel restarts slow start; allow the exit rule again. */
SEC("struct_ops/gr_set_state")
void BPF_PROG(gr_set_state, struct sock *sk, __u8 new_state)
{
	struct gentle_reno *ca = ca_of(sk);

	if (new_state == TCP_CA_Loss) {
		ca->ss_done = 0;
		ca->full_bw = 0;
		ca->flat_rounds = 0;
	}
}

/* Spurious loss detected (e.g. DSACK): restore the window, as Reno does. */
SEC("struct_ops/gr_undo_cwnd")
__u32 BPF_PROG(gr_undo_cwnd, struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);

	__sync_fetch_and_add(&stat_undo, 1);
	return tp->snd_cwnd > tp->prior_cwnd ? tp->snd_cwnd : tp->prior_cwnd;
}

SEC(".struct_ops")
struct tcp_congestion_ops gentle_reno = {
	.init       = (void *)gr_init,
	.ssthresh   = (void *)gr_ssthresh,
	.cong_avoid = (void *)gr_cong_avoid,
	.pkts_acked = (void *)gr_pkts_acked,
	.set_state  = (void *)gr_set_state,
	.undo_cwnd  = (void *)gr_undo_cwnd,
	.name       = "gentle_reno",
};
