/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef CR100_NEXMON_OWNERSHIP_H
#define CR100_NEXMON_OWNERSHIP_H

#include <stddef.h>
#include <stdint.h>

#define OWNERSHIP_IOCTL 430
#define OWNERSHIP_MAGIC 0x4f4e5243u
#define OWNERSHIP_VERSION 1u
#define TX_HEADROOM 202u
#define NOACK_STATS_IOCTL 432
#define NOACK_STATS_MAGIC 0x414e5243u /* CRNA */
#define NOACK_STATS_VERSION 1u
#define PACKET_TAG_NOACK 0x00001000u

/* Prefixes verified against bcm43455c0/7_45_206 structs.common.h and native
 * wl_send. No packet tags, links or ownership flags are copied to a TX clone. */
struct packet {
    uint32_t metadata0, metadata4;
    uint8_t *data;
    uint16_t len, flags;
    uint16_t dmapad;
    uint8_t data_off, ifidx;
    uint16_t nextby4, host_link;
    uint32_t tag_flags;
};

struct wlc {
    void *pub, *osh, *wl, *regs, *hw;
    uint8_t padding[0x20c - 5 * sizeof(void *)];
    uint32_t monitor;
};

struct wl {
    uint32_t unit;
    void *pub;
    struct wlc *wlc;
};

struct ownership_stats {
    uint32_t magic, version, size;
    uint32_t seen, cloned, source_freed, alloc_fail, malformed;
    uint32_t host_link_nonzero, forwarded, ioctl_reads, clone_free_on_error;
};

extern volatile struct ownership_stats ownership_stats;
struct noack_stats {
    uint32_t magic, version, size, capabilities;
    uint32_t probe_seen, probe_tagged, probe_rejected, probe_alloc_fail;
    uint32_t other_frames, ioctl_reads;
};
extern volatile struct noack_stats noack_stats;
int ownership_filter(struct wl *wl, struct packet *p);
int ownership_ioctl(struct wlc *wlc, int cmd, char *arg, int len, void *wlc_if);

#ifdef OWNERSHIP_HOST_TEST
struct packet *fw_pktget(void *osh, unsigned int len);
void fw_pktfree(void *osh, struct packet *p, int send);
int fw_inject(struct wl *wl, struct packet *p);
int fw_filter(struct wl *wl, struct packet *p);
int fw_ioctl(struct wlc *wlc, int cmd, char *arg, int len, void *wlc_if);
#else
_Static_assert(sizeof(void *) == 4, "Firmware ABI requires 32-bit pointers");
_Static_assert(offsetof(struct packet, data) == 8, "packet data ABI");
_Static_assert(offsetof(struct packet, len) == 0x0c, "packet length ABI");
_Static_assert(offsetof(struct packet, host_link) == 0x16, "PKTLINK ABI");
_Static_assert(offsetof(struct packet, tag_flags) == 0x18, "Packet tag ABI");
_Static_assert(offsetof(struct wl, wlc) == 8, "wl ABI");
_Static_assert(offsetof(struct wlc, osh) == 4, "osh ABI");
_Static_assert(offsetof(struct wlc, hw) == 0x10, "hardware context ABI");
_Static_assert(offsetof(struct wlc, monitor) == 0x20c, "monitor ABI");
#define fw_pktget ((struct packet *(*)(void *, unsigned int))(uintptr_t)0x9c04du)
#define fw_pktfree ((void (*)(void *, struct packet *, int))(uintptr_t)0x9c05du)
#define fw_inject ((int (*)(struct wl *, struct packet *))(uintptr_t)0x22c8b5u)
#define fw_filter ((int (*)(struct wl *, struct packet *))(uintptr_t)0x1a1ddfu)
#define fw_ioctl ((int (*)(struct wlc *, int, char *, int, void *))(uintptr_t)0x22c947u)
#endif

_Static_assert(sizeof(struct ownership_stats) == 48, "GET430 counter ABI");
_Static_assert(sizeof(struct noack_stats) == 40, "GET432 counter ABI");
#endif
