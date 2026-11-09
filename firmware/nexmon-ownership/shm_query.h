/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef CR100_PRIVATE_SHM_QUERY_H
#define CR100_PRIVATE_SHM_QUERY_H
#include <stdint.h>
#define SHM_QUERY_IOCTL 435
#define SHM_QUERY_MAGIC 0x514d4853u /* SHMQ */
#define SHM_QUERY_SIZE 48u
#define SHM_CLOCK_TABLE_BYTE_START 0x0eeeu
#define SHM_CLOCK_TABLE_WORDS 16u
struct wlc;
struct shm_query_reply {
    uint32_t magic, version, size, status;
    uint16_t words[SHM_CLOCK_TABLE_WORDS];
};
int shm_query(struct wlc *wlc, char *arg, int len);
#ifdef OWNERSHIP_HOST_TEST
uint16_t fw_read_shm(void *hw, unsigned int byte_offset);
#else
/* Exact 7.45.206 Thumb helper: r0=hw, r1=byte offset, selects SHM in
 * 0x1bda6e then reads the selected 16-bit data. The only MMIO write is
 * the read address selector; SHM data and hardware clocks are never written. */
#define fw_read_shm ((uint16_t (*)(void *, unsigned int))(uintptr_t)0x1bdf0du)
#endif
_Static_assert(sizeof(struct shm_query_reply) == SHM_QUERY_SIZE, "GET435 reply ABI");
#endif
