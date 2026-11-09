/* SPDX-License-Identifier: GPL-3.0-or-later
 * Native-post-accounting injection ownership fix for one exact Nexmon image.
 * The stock wl_send caller owns PKTLINK traversal and native->OS accounting.
 */
#include "ownership.h"
#include "tsf_query.h"
#include "shm_query.h"
#include "beacon_stamp.h"

volatile struct ownership_stats ownership_stats = {
    OWNERSHIP_MAGIC, OWNERSHIP_VERSION, sizeof(struct ownership_stats),
    0, 0, 0, 0, 0, 0, 0, 0, 0
};
volatile struct noack_stats noack_stats = {
    NOACK_STATS_MAGIC, NOACK_STATS_VERSION, sizeof(struct noack_stats), 1,
    0, 0, 0, 0, 0, 0
};

static int nonzero_unicast(const uint8_t *mac)
{
    unsigned int i;
    uint8_t any = 0;
    if (mac[0] & 1) return 0;
    for (i = 0; i < 6; ++i) any |= mac[i];
    return any != 0;
}

static int probe_needs_noack(const uint8_t *frame, unsigned int length)
{
    /* Only ordinary, complete, unprotected probe responses. Do not infer a
     * descriptor format or set flags on association, beacon or encrypted data. */
    if (frame[0] != 0x50) {
        ++noack_stats.other_frames;
        return 0;
    }
    ++noack_stats.probe_seen;
    if (length < 24 + 12 || (frame[1] & 0xc7) || (frame[22] & 0x0f) ||
            !nonzero_unicast(frame + 4) || !nonzero_unicast(frame + 10) ||
            !nonzero_unicast(frame + 16) || !(frame[32] | frame[33])) {
        ++noack_stats.probe_rejected;
        return 0;
    }
    return 1;
}

static void free_source(struct wlc *wlc, struct packet *p)
{
    ++ownership_stats.source_freed;
    fw_pktfree(wlc->osh, p, 0);
}

int ownership_filter(struct wl *wl, struct packet *p)
{
    struct wlc *wlc = wl->wlc;
    struct packet *copy;
    unsigned int i, length, radiotap_length;
    int noack;

    if (!wlc->monitor || !p || !p->data || p->len < 2 || p->data[0] || p->data[1]) {
        ++ownership_stats.forwarded;
        return fw_filter(wl, p);
    }
    ++ownership_stats.seen;

    /* At this call site stock wl_send has saved and cleared PKTLINK and run
     * its native->OS conversion. An unexpected link means this invariant failed:
     * retain the original stock ownership path rather than free a linked packet. */
    if (p->host_link) {
        ++ownership_stats.host_link_nonzero;
        ++ownership_stats.forwarded;
        return fw_filter(wl, p);
    }
    length = p->len;
    if (length < 8 || length > 65535u - TX_HEADROOM) goto malformed;
    radiotap_length = p->data[2] | ((unsigned int)p->data[3] << 8);
    /* The existing injector reads an 8-bit radiotap length. Reject unsupported
     * long headers instead of letting it strip a truncated length. ACK is the
     * shortest 802.11 frame accepted here (10 bytes). */
    if (radiotap_length < 8 || radiotap_length > 255 ||
            radiotap_length > length || length - radiotap_length < 10) goto malformed;

    noack = probe_needs_noack(p->data + radiotap_length, length - radiotap_length);
    copy = fw_pktget(wlc->osh, length + TX_HEADROOM);
    if (!copy) {
        ++ownership_stats.alloc_fail;
        if (noack) ++noack_stats.probe_alloc_fail;
        free_source(wlc, p);
        return 1;
    }
    if (!copy->data || copy->len < length + TX_HEADROOM) {
        ++ownership_stats.alloc_fail;
        ++ownership_stats.clone_free_on_error;
        if (noack) ++noack_stats.probe_alloc_fail;
        fw_pktfree(wlc->osh, copy, 0);
        free_source(wlc, p);
        return 1;
    }
    copy->data += TX_HEADROOM;
    copy->len = (uint16_t)length;
    for (i = 0; i < length; ++i) copy->data[i] = p->data[i];
    /* Exact 7.45.206 builder 0x1a5eda..0x1a5f02 maps this native packet-tag
     * bit to omission of MacTxControlLow 0x80. Metadata survives TX-param
     * insertion; the native builder, queues and completion path stay intact. */
    if (noack) {
        copy->tag_flags |= PACKET_TAG_NOACK;
        ++noack_stats.probe_tagged;
    }
    ++ownership_stats.cloned;
    free_source(wlc, p);
    /* The existing injector now owns only the independent allocation. Return
     * consumed=1 so stock wl_send continues its already-saved host chain. */
    fw_inject(wl, copy);
    return 1;

malformed:
    ++ownership_stats.malformed;
    free_source(wlc, p);
    return 1;
}

int ownership_ioctl(struct wlc *wlc, int cmd, char *arg, int len, void *wlc_if)
{
    uint32_t snapshot[sizeof(struct ownership_stats) / sizeof(uint32_t)];
    const volatile uint32_t *values = (const volatile uint32_t *)&ownership_stats;
    unsigned int i;
    if (cmd == BEACON_STAMP_IOCTL) return beacon_stamp_query(arg, len);
    if (cmd == SHM_QUERY_IOCTL) return shm_query(wlc, arg, len);
    if (cmd == TSF_QUERY_IOCTL) return tsf_query(wlc, arg, len);
    if (cmd == NOACK_STATS_IOCTL) {
        uint32_t noack_snapshot[sizeof(struct noack_stats) / sizeof(uint32_t)];
        const volatile uint32_t *noack_values = (const volatile uint32_t *)&noack_stats;
        if (!arg || len < (int)sizeof(struct noack_stats)) return -14;
        ++noack_stats.ioctl_reads;
        for (i = 0; i < sizeof(noack_snapshot) / sizeof(noack_snapshot[0]); ++i)
            noack_snapshot[i] = noack_values[i];
        for (i = 0; i < sizeof(noack_snapshot); ++i) arg[i] = ((const char *)noack_snapshot)[i];
        return 0;
    }
    if (cmd != OWNERSHIP_IOCTL) return fw_ioctl(wlc, cmd, arg, len, wlc_if);
    if (!arg || len < (int)sizeof(struct ownership_stats)) return -14; /* BCME_BUFTOOSHORT */
    ++ownership_stats.ioctl_reads;
    for (i = 0; i < sizeof(snapshot) / sizeof(snapshot[0]); ++i) snapshot[i] = values[i];
    for (i = 0; i < sizeof(snapshot); ++i) arg[i] = ((const char *)snapshot)[i];
    return 0;
}
