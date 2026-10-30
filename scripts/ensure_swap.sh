#!/usr/bin/env bash
# Idempotently ensure a swap file exists so the 1 GB VM stops thrashing.
# Never fails the caller: every problem is reported and the script exits 0.
set -u

SWAP_FILE="${SWAP_FILE:-/swapfile}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

if ! command -v swapon >/dev/null 2>&1; then
  echo "swap: swapon not available; skipped"
  exit 0
fi

if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then
  echo "swap: already active"
  swapon --show 2>/dev/null || true
  exit 0
fi

if ! sudo -n true 2>/dev/null; then
  echo "swap: passwordless sudo unavailable; skipped"
  exit 0
fi

if [ ! -f "$SWAP_FILE" ]; then
  sudo -n fallocate -l "$SWAP_SIZE" "$SWAP_FILE" \
    || sudo -n dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048 status=none \
    || { echo "swap: could not allocate $SWAP_FILE"; exit 0; }
  sudo -n chmod 600 "$SWAP_FILE"
  sudo -n mkswap "$SWAP_FILE" >/dev/null || { echo "swap: mkswap failed"; exit 0; }
fi

sudo -n swapon "$SWAP_FILE" || { echo "swap: swapon failed"; exit 0; }
if ! grep -q "^$SWAP_FILE " /etc/fstab 2>/dev/null; then
  echo "$SWAP_FILE none swap sw 0 0" | sudo -n tee -a /etc/fstab >/dev/null || true
fi
# Prefer RAM; only spill cold pages to swap.
sudo -n sysctl -q vm.swappiness=10 >/dev/null 2>&1 || true
echo "swap: enabled $SWAP_FILE ($SWAP_SIZE)"
swapon --show 2>/dev/null || true
free -m 2>/dev/null || true
exit 0
