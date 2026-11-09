/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "ownership.h"
#include "tsf_query.h"

int tsf_read(struct wlc *wlc, uint32_t *low, uint32_t *high)
{
    uint32_t previous_low, previous_high, current_low, current_high;
    unsigned int attempt;
    if (low) *low = 0;
    if (high) *high = 0;
    if (!low || !high) return 0;
    if (!wlc || !wlc->hw ||
            !*((const volatile uint8_t *)wlc->hw + TSF_QUERY_HW_UP_OFFSET)) return 0;

    /* The native helper reads LO then HI. Accept the newer of two samples
     * only when their high words agree and low words did not move backward.
     * One extra sample handles a low-word wrap during either native read.
     * Repeated changes/resets fail closed instead of returning a mixed clock. */
    fw_tsf_read(wlc, &previous_low, &previous_high);
    for (attempt = 0; attempt < 2; ++attempt) {
        fw_tsf_read(wlc, &current_low, &current_high);
        if (current_high == previous_high && current_low >= previous_low) {
            *low = current_low;
            *high = current_high;
            return 1;
        }
        previous_low = current_low;
        previous_high = current_high;
    }
    return 0;
}

int tsf_query(struct wlc *wlc, char *arg, int len)
{
    struct tsf_query_reply reply = {TSF_QUERY_MAGIC, TSF_QUERY_VERSION, TSF_QUERY_SIZE, 0, 0, 0};
    const uint8_t *bytes = (const uint8_t *)&reply;
    unsigned int i;
    if (!arg || len < (int)sizeof(reply)) return -14; /* BCME_BUFTOOSHORT */
    reply.status = (uint32_t)tsf_read(wlc, &reply.low, &reply.high);
    for (i = 0; i < sizeof(reply); ++i) arg[i] = (char)bytes[i];
    return 0;
}
