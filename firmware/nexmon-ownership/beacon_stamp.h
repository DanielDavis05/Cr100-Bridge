/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef CR100_PRIVATE_BEACON_STAMP_H
#define CR100_PRIVATE_BEACON_STAMP_H
#include "ownership.h"
#define BEACON_STAMP_IOCTL 436
#define BEACON_STAMP_MAGIC 0x51545342u /* BSTQ */
struct beacon_stamp_stats {
    uint32_t magic, version, size, capabilities;
    uint32_t sequence, calls, beacon_candidates, stamped, already_stamped, rejected_headers, ioctl_reads, last_valid;
    uint32_t last_low, last_high_before, last_high_after, last_bss_index, last_prefix, last_length_before, last_length_after, reserved;
};
extern volatile struct beacon_stamp_stats beacon_stamp_stats;
int beacon_stamp_build(struct wlc *, struct packet *, void *, int, unsigned int, unsigned int,
                       unsigned int, int, void *, unsigned int, uint16_t *);
int beacon_stamp_query(char *, int);
#ifdef OWNERSHIP_HOST_TEST
void *fw_injection_scb(struct wlc *);
int fw_descriptor_build(struct wlc *, struct packet *, void *, int, unsigned int, unsigned int,
                        unsigned int, int, void *, unsigned int, uint16_t *);
#else
/* Exact sendframe 0x22cfce/0x22cfe4 loads band+hwrs_scb at these offsets.
 * Use explicit fixed loads so the production prefix's padding is not retyped. */
static inline void *fw_injection_scb(struct wlc *wlc)
{
    void *band, *scb;
    __asm__ volatile ("ldr %0, [%1, #0x20]" : "=r"(band) : "r"(wlc) : "memory");
    if (!band) return 0;
    __asm__ volatile ("ldr %0, [%1, #0x18]" : "=r"(scb) : "r"(band) : "memory");
    return scb;
}
#define fw_descriptor_build ((int (*)(struct wlc *, struct packet *, void *, int, unsigned int, unsigned int, \
    unsigned int, int, void *, unsigned int, uint16_t *))(uintptr_t)0x1a63edu)
#endif
_Static_assert(sizeof(struct beacon_stamp_stats) == 80, "GET436 reply ABI");
#endif
