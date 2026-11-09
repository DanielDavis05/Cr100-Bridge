/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef CR100_DIAGNOSTIC_TSF_QUERY_H
#define CR100_DIAGNOSTIC_TSF_QUERY_H

#include <stdint.h>

#define TSF_QUERY_IOCTL 433
#define TSF_QUERY_MAGIC 0x51465354u /* little-endian bytes TSFQ */
#define TSF_QUERY_VERSION 1u
#define TSF_QUERY_SIZE 24u
#define TSF_QUERY_HW_UP_OFFSET 0xacu

struct wlc;

struct tsf_query_reply {
    uint32_t magic, version, size, status, low, high;
};

/* Return 1 for a bounded, coherent sample while the hardware is up, else 0.
 * Outputs are zero when unavailable. This never writes a radio register. */
int tsf_read(struct wlc *wlc, uint32_t *low, uint32_t *high);
int tsf_query(struct wlc *wlc, char *arg, int len);

#ifdef OWNERSHIP_HOST_TEST
void fw_tsf_read(struct wlc *wlc, uint32_t *low, uint32_t *high);
#else
/* Exact owned-image wrapper: 0x1a96ec loads wlc->hw (+0x10), then branches
 * to 0x1bdd54. That function only reads regs (+0x88) TSF LO/HI (+0x180/184)
 * and writes the two caller outputs. */
#define fw_tsf_read ((void (*)(struct wlc *, uint32_t *, uint32_t *))(uintptr_t)0x1a96edu)
#endif

_Static_assert(sizeof(struct tsf_query_reply) == TSF_QUERY_SIZE, "GET433 counter ABI");
#endif
