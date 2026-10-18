# Build / load the Gentle Reno eBPF congestion control.
#
#   make            build gentle_reno.bpf.o (generates vmlinux.h from the running kernel)
#   sudo make load  register it with the kernel  -> "gentle_reno" becomes selectable
#   make status     show whether it is registered
#   sudo make stats show the program's counters (loss events, slow-start exits, ...)
#   sudo make unload
#
# Requirements: clang (>= 12), libbpf headers (libbpf-dev), bpftool, and a kernel
# with CONFIG_BPF_SYSCALL, CONFIG_BPF_JIT, CONFIG_DEBUG_INFO_BTF (/sys/kernel/btf/vmlinux).

CLANG   ?= clang
# bpftool: use the one on PATH if it works, otherwise any installed linux-tools copy
# (Ubuntu's /usr/sbin/bpftool wrapper fails when linux-tools-$(uname -r) is missing).
BPFTOOL ?= $(shell bpftool version >/dev/null 2>&1 && echo bpftool || ls /usr/lib/linux-tools-*/bpftool 2>/dev/null | tail -1)
ARCH    := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/')

OBJ := gentle_reno.bpf.o

all: $(OBJ)

vmlinux.h:
	@test -n "$(BPFTOOL)" || (echo "bpftool not found: sudo apt install linux-tools-$$(uname -r) linux-tools-common" && false)
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(OBJ): gentle_reno.bpf.c vmlinux.h
	$(CLANG) -O2 -g -Wall -target bpf -D__TARGET_ARCH_$(ARCH) -I. -c $< -o $@

load: $(OBJ)
	-$(BPFTOOL) struct_ops unregister name gentle_reno 2>/dev/null
	$(BPFTOOL) struct_ops register $(OBJ)
	@cat /proc/sys/net/ipv4/tcp_available_congestion_control

unload:
	$(BPFTOOL) struct_ops unregister name gentle_reno

status:
	@cat /proc/sys/net/ipv4/tcp_available_congestion_control
	@$(BPFTOOL) struct_ops show 2>/dev/null | grep -A0 gentle_reno || echo "gentle_reno not registered"

stats:
	@$(BPFTOOL) map dump name $$( $(BPFTOOL) map show | awk '/name gentle_r.*bss/ {print $$4; exit}' ) 2>/dev/null \
	  || echo "no counters found (is it loaded?)"

clean:
	rm -f $(OBJ) vmlinux.h

.PHONY: all load unload status stats clean
