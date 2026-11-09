/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "beacon_stamp.h"
#include "tsf_query.h"
volatile struct beacon_stamp_stats beacon_stamp_stats = {
    BEACON_STAMP_MAGIC, 1, sizeof(struct beacon_stamp_stats), 1,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
};

static int valid_beacon(const uint8_t *f, unsigned int length)
{
    unsigned int i, offset;
    uint8_t any = 0;
    if (!f || length < 38 || length > 512 || f[0] != 0x80 || f[1] || f[2] || f[3] ||
            (f[22] & 0x0f) || f[32] != 100 || f[33] || f[36] != 0 || f[37] > 32) return 0;
    for (i = 0; i < 6; ++i) {
        if (f[4 + i] != 0xff || f[10 + i] != f[16 + i]) return 0;
        any |= f[10 + i];
    }
    if (!any || (f[10] & 1)) return 0;
    /* Walk only bounded public beacon IE headers; reject truncated bodies. */
    for (offset = 36; offset < length;) {
        if (length - offset < 2 || f[offset + 1] > length - offset - 2) return 0;
        offset += 2 + f[offset + 1];
    }
    return 1;
}

int beacon_stamp_build(struct wlc *wlc, struct packet *packet, void *scb, int preamble,
                       unsigned int frag, unsigned int nfrag, unsigned int fifo, int next_length,
                       void *key, unsigned int rate, uint16_t *txh_offset)
{
    uint8_t *frame = packet ? packet->data : 0;
    unsigned int length = packet ? packet->len : 0, prefix = 0;
    int eligible = wlc && packet && wlc->monitor && wlc->hw &&
        *((const volatile uint8_t *)wlc->hw + TSF_QUERY_HW_UP_OFFSET) &&
        scb && scb == fw_injection_scb(wlc) && !preamble && !frag && nfrag == 1 &&
        fifo == 1 && !next_length && !key && !txh_offset &&
        (packet->tag_flags & 0x84) == 4 && valid_beacon(packet->data, length);
    int result = fw_descriptor_build(wlc, packet, scb, preamble, frag, nfrag, fifo,
                                    next_length, key, rate, txh_offset);
    uint8_t *header;
    unsigned int low = 0, high_before = 0, high_after = 0, index = 0, valid = 0;
    ++beacon_stamp_stats.sequence;
    ++beacon_stamp_stats.calls;
    if (!eligible) goto done;
    ++beacon_stamp_stats.beacon_candidates;
    /* Native callers 0x25c98 / 0x1a6d30 access this same live packet after the
     * original header wrapper returns, before hardware enqueue. No packet is
     * retained, freed, queued, or inspected after this wrapper returns. */
    if (!packet->data || !packet->len || !(packet->tag_flags & 0x80)) goto rejected;
    if (!((const uint8_t *)wlc)[0x674]) prefix = (packet->data[0] & 1) ? 16 : 4;
    if (packet->len != length + prefix + 124 ||
            (uintptr_t)packet->data + prefix + 124 != (uintptr_t)frame) goto rejected;
    header = packet->data + prefix;
    if ((header[2] & 1) || !valid_beacon(header + 124, length)) goto rejected;
    low = header[2] | ((unsigned int)header[3] << 8);
    high_before = header[4] | ((unsigned int)header[5] << 8);
    index = header[100] & 3;
    if (high_before & 0x80) ++beacon_stamp_stats.already_stamped;
    else {
        /* The single functional write: enable hardware beacon timestamping.
         * Do not touch low-word ACK policy, frame bytes or other descriptor bits. */
        header[4] |= 0x80;
        ++beacon_stamp_stats.stamped;
    }
    high_after = header[4] | ((unsigned int)header[5] << 8);
    valid = 1;
    goto sample;
rejected:
    ++beacon_stamp_stats.rejected_headers;
sample:
    beacon_stamp_stats.last_valid = valid;
    beacon_stamp_stats.last_low = low;
    beacon_stamp_stats.last_high_before = high_before;
    beacon_stamp_stats.last_high_after = high_after;
    beacon_stamp_stats.last_bss_index = index;
    beacon_stamp_stats.last_prefix = prefix;
    beacon_stamp_stats.last_length_before = length;
    beacon_stamp_stats.last_length_after = packet->len;
done:
    ++beacon_stamp_stats.sequence;
    return result;
}

int beacon_stamp_query(char *arg, int len)
{
    uint32_t snapshot[sizeof(struct beacon_stamp_stats) / 4];
    const volatile uint32_t *source = (const volatile uint32_t *)&beacon_stamp_stats;
    unsigned int attempt, i;
    if (!arg || len < (int)sizeof(struct beacon_stamp_stats)) return -14;
    ++beacon_stamp_stats.ioctl_reads;
    for (attempt = 0; attempt < 3; ++attempt) {
        uint32_t sequence = beacon_stamp_stats.sequence;
        if (sequence & 1) continue;
        for (i = 0; i < sizeof(snapshot) / 4; ++i) snapshot[i] = source[i];
        if (sequence != beacon_stamp_stats.sequence) continue;
        for (i = 0; i < sizeof(snapshot); ++i) arg[i] = ((const char *)snapshot)[i];
        return 0;
    }
    return -16;
}
