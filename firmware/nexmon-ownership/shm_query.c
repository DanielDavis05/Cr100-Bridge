/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "ownership.h"
#include "tsf_query.h"
#include "shm_query.h"

int shm_query(struct wlc *wlc, char *arg, int len)
{
    struct shm_query_reply reply;
    volatile uint16_t *clear = reply.words;
    uint16_t first[SHM_CLOCK_TABLE_WORDS], second[SHM_CLOCK_TABLE_WORDS];
    const uint8_t *bytes = (const uint8_t *)&reply;
    unsigned int i, pass;
    void *hw;
    if (!arg || len < (int)sizeof(reply)) return -14;
    reply.magic = SHM_QUERY_MAGIC; reply.version = 1; reply.size = SHM_QUERY_SIZE; reply.status = 0;
    for (i = 0; i < SHM_CLOCK_TABLE_WORDS; ++i) clear[i] = 0;
    if (!wlc || !(hw = wlc->hw)) goto output;
    for (pass = 0; pass < 2; ++pass) {
        if (!*((const volatile uint8_t *)hw + TSF_QUERY_HW_UP_OFFSET)) goto output;
        for (i = 0; i < SHM_CLOCK_TABLE_WORDS; ++i) {
            if (!*((const volatile uint8_t *)hw + TSF_QUERY_HW_UP_OFFSET)) goto output;
            uint16_t value = fw_read_shm(hw, SHM_CLOCK_TABLE_BYTE_START + 2 * i);
            if (pass) second[i] = value;
            else first[i] = value;
        }
    }
    if (!*((const volatile uint8_t *)hw + TSF_QUERY_HW_UP_OFFSET)) goto output;
    for (i = 0; i < SHM_CLOCK_TABLE_WORDS; ++i)
        if (first[i] != second[i]) goto output;
    for (i = 0; i < SHM_CLOCK_TABLE_WORDS; ++i) reply.words[i] = second[i];
    reply.status = 1;
output:
    for (i = 0; i < sizeof(reply); ++i) arg[i] = (char)bytes[i];
    return 0;
}
