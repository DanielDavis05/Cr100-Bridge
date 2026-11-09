/* SPDX-License-Identifier: GPL-3.0-or-later
 * Exercise the exact firmware C with checked fake allocator/free/injection APIs. */
#include "ownership.h"
#include "tsf_query.h"
#include "beacon_stamp.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

#define COUNT 4
#define CAPACITY 2048
static struct packet sources[COUNT], copies[COUNT];
static uint8_t source_data[COUNT][CAPACITY], copy_data[COUNT][CAPACITY], expected[COUNT][CAPACITY];
static unsigned int expected_len[COUNT], source_frees[COUNT], copy_frees[COUNT], normalized[COUNT];
static uint32_t expected_tags[COUNT];
static unsigned int allocations, injections, fail_alloc, bad_allocation, account, forwarded, ioctl_forwarded;
static void *expected_osh = (void *)(uintptr_t)0x1234;
static struct wlc wlc;
static struct wl wl;
/* The ownership harness mocks fw_inject; native descriptor construction is
 * exercised separately by test_beacon_stamp.c using the same production C. */
void *fw_injection_scb(struct wlc *context) { (void)context; return 0; }
int fw_descriptor_build(struct wlc *context, struct packet *p, void *scb, int preamble,
        unsigned int frag, unsigned int nfrag, unsigned int fifo, int next, void *key,
        unsigned int rate, uint16_t *offset)
{
    (void)context; (void)p; (void)scb; (void)preamble; (void)frag; (void)nfrag;
    (void)fifo; (void)next; (void)key; (void)rate; (void)offset;
    assert(!"Unexpected native builder call in ownership mock"); return 0;
}
uint16_t fw_read_shm(void *hw, unsigned int offset)
{
    (void)hw; (void)offset; assert(!"Unexpected SHM call in ownership mock"); return 0;
}

static int packet_index(struct packet *p, struct packet *table)
{
    int i;
    for (i = 0; i < COUNT; ++i) if (p == &table[i]) return i;
    return -1;
}

static void reset(void)
{
    memset(sources, 0, sizeof(sources));
    memset(copies, 0, sizeof(copies));
    memset(source_frees, 0, sizeof(source_frees));
    memset(copy_frees, 0, sizeof(copy_frees));
    memset(normalized, 0, sizeof(normalized));
    allocations = injections = fail_alloc = bad_allocation = account = forwarded = ioctl_forwarded = 0;
    for (unsigned int i = 0; i < COUNT; ++i) expected_tags[i] = 0x200000;
    ownership_stats = (struct ownership_stats){OWNERSHIP_MAGIC, OWNERSHIP_VERSION, sizeof(ownership_stats),
                                              0, 0, 0, 0, 0, 0, 0, 0, 0};
    noack_stats = (struct noack_stats){NOACK_STATS_MAGIC, NOACK_STATS_VERSION, sizeof(noack_stats), 1,
                                      0, 0, 0, 0, 0, 0};
    memset(&wlc, 0, sizeof(wlc));
    memset(&wl, 0, sizeof(wl));
    wlc.osh = expected_osh;
    wlc.monitor = 1;
    wl.wlc = &wlc;
}

static struct packet *raw(unsigned int index)
{
    struct packet *p = &sources[index];
    unsigned int i;
    p->data = source_data[index];
    p->len = 10 + 24;
    p->metadata0 = 0xaabbccddu;
    p->metadata4 = 0x11223344u;
    p->flags = 0xfeed;
    for (i = 0; i < p->len; ++i) p->data[i] = (uint8_t)(i + index);
    p->data[0] = p->data[1] = p->data[3] = 0;
    p->data[2] = 10;
    p->data[4] = 6; p->data[5] = p->data[6] = p->data[7] = p->data[8] = 0;
    p->data[9] = 48; /* 24 Mb/s, unchanged raw monitor injection protocol */
    expected_len[index] = p->len;
    memcpy(expected[index], p->data, p->len);
    return p;
}

static void convert_native(struct packet *p)
{
    int index = packet_index(p, sources);
    assert(index >= 0 && !normalized[index]);
    p->host_link = 0;
    normalized[index] = 1;
    ++account;
}

struct packet *fw_pktget(void *osh, unsigned int length)
{
    struct packet *p;
    unsigned int index = allocations++;
    assert(osh == expected_osh && index < COUNT && length <= CAPACITY);
    if (fail_alloc) return 0;
    p = &copies[index];
    p->data = copy_data[index];
    p->len = (uint16_t)(bad_allocation ? length - 1 : length);
    p->metadata0 = 0x40000000u + index;
    p->tag_flags = 0x200000; /* Fresh unrelated metadata must survive marking. */
    ++account;
    return p;
}

void fw_pktfree(void *osh, struct packet *p, int send)
{
    int index = packet_index(p, sources);
    assert(osh == expected_osh && send == 0 && account > 0);
    if (index >= 0) {
        assert(normalized[index] && !p->host_link && ++source_frees[index] == 1);
        /* Simulate immediate host-buffer reuse before the queued clone transmits. */
        memset(p->data, 0xcc, p->len < CAPACITY ? p->len : CAPACITY);
    } else {
        index = packet_index(p, copies);
        assert(index >= 0 && ++copy_frees[index] == 1);
    }
    --account;
}

int fw_inject(struct wl *context, struct packet *p)
{
    int index = packet_index(p, copies);
    assert(context == &wl && index >= 0 && source_frees[index] == 1);
    assert(p->data == copy_data[index] + TX_HEADROOM);
    assert(p->len == expected_len[index] && !p->host_link && !p->nextby4);
    assert(p->metadata0 == 0x40000000u + (unsigned int)index && p->flags == 0);
    assert(p->tag_flags == expected_tags[index]);
    assert(memcmp(p->data, expected[index], p->len) == 0);
    ++injections;
    return 0;
}

int fw_filter(struct wl *context, struct packet *p)
{
    assert(context == &wl && p == &sources[0]);
    ++forwarded;
    return 17;
}

int fw_ioctl(struct wlc *context, int cmd, char *arg, int length, void *wlc_if)
{
    assert(context == &wlc && cmd == 511 && arg == (char *)sources && length == 9);
    assert(wlc_if == expected_osh);
    ++ioctl_forwarded;
    return 123;
}

static void test_host_chain_and_delayed_tx(void)
{
    struct packet *p, *next;
    unsigned int i;
    reset(); raw(0); raw(1);
    sources[0].host_link = 2; /* Native pktid table resolves this to sources[1]. */
    p = &sources[0];
    while (p) {
        next = p->host_link ? &sources[p->host_link - 1] : 0;
        convert_native(p);
        assert(ownership_filter(&wl, p) == 1);
        p = next;
    }
    assert(allocations == 2 && injections == 2 && account == 2);
    assert(ownership_stats.seen == 2 && ownership_stats.cloned == 2 && ownership_stats.source_freed == 2);
    assert(ownership_stats.host_link_nonzero == 0 && !ownership_stats.forwarded);
    for (i = 0; i < 2; ++i) {
        assert(source_frees[i] == 1 && copy_frees[i] == 0);
        assert(memcmp(copies[i].data, expected[i], expected_len[i]) == 0);
        fw_pktfree(expected_osh, &copies[i], 0);
    }
    assert(account == 0);
}

static void test_allocation_failures(void)
{
    unsigned int invalid;
    for (invalid = 0; invalid < 2; ++invalid) {
        reset(); raw(0); convert_native(&sources[0]);
        if (invalid) bad_allocation = 1;
        else fail_alloc = 1;
        assert(ownership_filter(&wl, &sources[0]) == 1);
        assert(ownership_stats.alloc_fail == 1 && ownership_stats.source_freed == 1);
        assert(ownership_stats.clone_free_on_error == invalid);
        assert(injections == 0 && account == 0);
    }
}

static void test_malformed_lengths(void)
{
    static const uint16_t lengths[] = {2, 3, 7, 8, 17, 65535};
    unsigned int i;
    for (i = 0; i < sizeof(lengths) / sizeof(lengths[0]); ++i) {
        reset(); raw(0); sources[0].len = lengths[i]; convert_native(&sources[0]);
        assert(ownership_filter(&wl, &sources[0]) == 1);
        assert(ownership_stats.malformed == 1 && ownership_stats.source_freed == 1);
        assert(allocations == 0 && injections == 0 && account == 0);
    }
    reset(); raw(0); sources[0].data[2] = 0; convert_native(&sources[0]);
    assert(ownership_filter(&wl, &sources[0]) == 1 && ownership_stats.malformed == 1);
    reset(); raw(0); sources[0].data[3] = 1; convert_native(&sources[0]);
    assert(ownership_filter(&wl, &sources[0]) == 1 && ownership_stats.malformed == 1);
}

static void test_forwarding_and_invariant_check(void)
{
    reset(); raw(0); sources[0].data[0] = 1;
    assert(ownership_filter(&wl, &sources[0]) == 17 && forwarded == 1);
    assert(allocations == 0 && source_frees[0] == 0);
    reset(); raw(0); wlc.monitor = 0;
    assert(ownership_filter(&wl, &sources[0]) == 17 && forwarded == 1);
    reset(); raw(0); sources[0].host_link = 3;
    assert(ownership_filter(&wl, &sources[0]) == 17 && ownership_stats.host_link_nonzero == 1);
    assert(allocations == 0 && source_frees[0] == 0);
}

static void test_get_only_counter_snapshot_and_ioctl_forwarding(void)
{
    uint8_t buffer[64];
    struct ownership_stats result;
    unsigned int i;
    reset(); memset(buffer, 0xa5, sizeof(buffer));
    assert(ownership_ioctl(&wlc, OWNERSHIP_IOCTL, (char *)buffer, 47, 0) == -14);
    assert(ownership_ioctl(&wlc, OWNERSHIP_IOCTL, 0, 48, 0) == -14);
    assert(!ownership_stats.ioctl_reads);
    assert(ownership_ioctl(&wlc, OWNERSHIP_IOCTL, (char *)buffer, 64, 0) == 0);
    memcpy(&result, buffer, sizeof(result));
    assert(result.magic == OWNERSHIP_MAGIC && result.version == 1 && result.size == 48 && result.ioctl_reads == 1);
    for (i = 48; i < sizeof(buffer); ++i) assert(buffer[i] == 0xa5);
    assert(ownership_ioctl(&wlc, 511, (char *)sources, 9, expected_osh) == 123 && ioctl_forwarded == 1);
}

static struct packet *probe(unsigned int index)
{
    struct packet *p = raw(index);
    uint8_t *f = p->data + 10;
    p->len = 10 + 24 + 12;
    memset(f, 0, 36);
    f[0] = 0x50;
    f[4] = f[10] = f[16] = 2;
    f[9] = 1; f[15] = f[21] = 2;
    f[32] = 100;
    return p;
}

static void inject_prepared(unsigned int index, int expected_noack)
{
    expected_len[index] = sources[index].len;
    memcpy(expected[index], sources[index].data, sources[index].len);
    if (expected_noack) expected_tags[index] |= PACKET_TAG_NOACK;
    convert_native(&sources[index]);
    assert(ownership_filter(&wl, &sources[index]) == 1);
}

static void test_noack_probe_only_preserves_raw_bytes_and_unrelated_metadata(void)
{
    reset(); probe(0); raw(1); raw(2); raw(3);
    sources[1].data[10] = 0x80; /* Beacon. */
    sources[2].data[10] = 0x08; sources[2].data[11] = 0x42; /* Protected FromDS data. */
    sources[3].data[10] = 0x10; /* Association response. */
    inject_prepared(0, 1);
    for (unsigned int i = 1; i < COUNT; ++i) inject_prepared(i, 0);
    assert(noack_stats.probe_seen == 1 && noack_stats.probe_tagged == 1 && !noack_stats.probe_rejected);
    assert(noack_stats.other_frames == 3 && !noack_stats.probe_alloc_fail);
    assert(ownership_stats.seen == COUNT && ownership_stats.cloned == COUNT && ownership_stats.source_freed == COUNT);
    for (unsigned int i = 0; i < COUNT; ++i) fw_pktfree(expected_osh, &copies[i], 0);
    assert(account == 0);
}

static void test_truncated_multicast_and_invalid_probe_headers_are_not_marked(void)
{
    for (unsigned int scenario = 0; scenario < 10; ++scenario) {
        struct packet *p;
        reset(); p = probe(0);
        switch (scenario) {
            case 0: p->len = 10 + 10; break;
            case 1: p->len = 10 + 24 + 11; break;
            case 2: p->data[10 + 4] = 3; break;
            case 3: memset(p->data + 10 + 4, 0, 6); break;
            case 4: p->data[10 + 1] = 0x40; break;
            case 5: p->data[10 + 1] = 0x02; break;
            case 6: p->data[10 + 22] = 1; break;
            case 7: p->data[10 + 10] = 3; break;
            case 8: p->data[10 + 16] = 3; break;
            case 9: p->data[10 + 32] = 0; break;
        }
        inject_prepared(0, 0);
        assert(noack_stats.probe_seen == 1 && !noack_stats.probe_tagged && noack_stats.probe_rejected == 1);
        assert(ownership_stats.cloned == 1 && injections == 1);
        fw_pktfree(expected_osh, &copies[0], 0);
        assert(account == 0);
    }
}

static void test_probe_allocation_failure_and_bounded_noack_counters(void)
{
    uint8_t buffer[48];
    struct noack_stats result;
    reset(); probe(0); convert_native(&sources[0]); fail_alloc = 1;
    assert(ownership_filter(&wl, &sources[0]) == 1);
    assert(noack_stats.probe_seen == 1 && noack_stats.probe_alloc_fail == 1 && !noack_stats.probe_tagged);
    assert(source_frees[0] == 1 && !injections && account == 0);
    memset(buffer, 0xa5, sizeof(buffer));
    assert(ownership_ioctl(&wlc, NOACK_STATS_IOCTL, 0, 40, 0) == -14);
    assert(ownership_ioctl(&wlc, NOACK_STATS_IOCTL, (char *)buffer, 39, 0) == -14);
    assert(noack_stats.ioctl_reads == 0);
    assert(ownership_ioctl(&wlc, NOACK_STATS_IOCTL, (char *)buffer, sizeof(buffer), 0) == 0);
    memcpy(&result, buffer, sizeof(result));
    assert(result.magic == NOACK_STATS_MAGIC && result.version == 1 && result.size == 40 && result.capabilities == 1);
    assert(result.probe_seen == 1 && result.probe_alloc_fail == 1 && result.ioctl_reads == 1);
    for (unsigned int i = 40; i < sizeof(buffer); ++i) assert(buffer[i] == 0xa5);
}

static unsigned int clock_reads, clock_case;
static uint8_t hardware[0xc0];

void fw_tsf_read(struct wlc *context, uint32_t *low, uint32_t *high)
{
    assert(context == &wlc && clock_reads < 3);
    ++clock_reads;
    if (clock_case == 0) { *low = 100 + clock_reads; *high = 7; }
    else if (clock_case == 1) { *low = clock_reads == 1 ? UINT32_MAX : clock_reads; *high = clock_reads == 1 ? 7 : 8; }
    else { *low = clock_reads; *high = clock_reads; }
}

static void test_coherent_readonly_clock_and_reply_bounds(void)
{
    uint8_t allocation[30], before[30];
    struct tsf_query_reply result;
    uint32_t low, high;
    reset(); memset(allocation, 0xa5, sizeof(allocation)); memcpy(before, allocation, sizeof(before));
    clock_reads = 0;
    assert(ownership_ioctl(&wlc, TSF_QUERY_IOCTL, 0, 24, 0) == -14);
    assert(ownership_ioctl(&wlc, TSF_QUERY_IOCTL, (char *)allocation, 23, 0) == -14);
    assert(!clock_reads && !memcmp(allocation, before, sizeof(before)));
    assert(ownership_ioctl(&wlc, TSF_QUERY_IOCTL, (char *)allocation + 1, 24, 0) == 0);
    memcpy(&result, allocation + 1, sizeof(result));
    assert(result.magic == TSF_QUERY_MAGIC && result.version == 1 && result.size == 24);
    assert(!result.status && !result.low && !result.high && !clock_reads);
    assert(allocation[0] == 0xa5 && allocation[25] == 0xa5);
    memset(hardware, 0, sizeof(hardware)); wlc.hw = hardware;
    assert(!tsf_read(&wlc, &low, &high) && !clock_reads);
    hardware[TSF_QUERY_HW_UP_OFFSET] = 1;
    assert(!tsf_read(0, &low, &high) && !clock_reads);
    assert(!tsf_read(&wlc, 0, &high) && !clock_reads);
    for (clock_case = 0; clock_case < 3; ++clock_case) {
        clock_reads = 0;
        assert(ownership_ioctl(&wlc, TSF_QUERY_IOCTL, (char *)allocation + 1, 24, 0) == 0);
        memcpy(&result, allocation + 1, sizeof(result));
        assert(result.status == (clock_case < 2));
        if (clock_case == 0) assert(result.low == 102 && result.high == 7 && clock_reads == 2);
        else if (clock_case == 1) assert(result.low == 3 && result.high == 8 && clock_reads == 3);
        else assert(!result.low && !result.high && clock_reads == 3);
        assert(hardware[TSF_QUERY_HW_UP_OFFSET] == 1 && allocation[0] == 0xa5 && allocation[25] == 0xa5);
    }
}

int main(void)
{
    test_host_chain_and_delayed_tx();
    test_allocation_failures();
    test_malformed_lengths();
    test_forwarding_and_invariant_check();
    test_get_only_counter_snapshot_and_ioctl_forwarding();
    test_noack_probe_only_preserves_raw_bytes_and_unrelated_metadata();
    test_truncated_multicast_and_invalid_probe_headers_are_not_marked();
    test_probe_allocation_failure_and_bounded_noack_counters();
    test_coherent_readonly_clock_and_reply_bounds();
    puts("ownership harness: all invariants passed");
    return 0;
}
