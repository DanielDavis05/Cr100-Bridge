#include "ownership.h"
#include "tsf_query.h"
#include "shm_query.h"
#include <assert.h>
#include <string.h>
#include <stdio.h>
static struct wlc context;
static uint8_t hardware[0xc0], original_hardware[0xc0];
static unsigned int reads, mismatch, down_after;
uint16_t fw_read_shm(void *hw, unsigned int offset)
{
    unsigned int index = reads % SHM_CLOCK_TABLE_WORDS;
    assert(hw == hardware && reads < 32);
    assert(offset == SHM_CLOCK_TABLE_BYTE_START + 2 * index);
    ++reads;
    if (down_after == reads) hardware[TSF_QUERY_HW_UP_OFFSET] = 0;
    return (uint16_t)(0xa100 + index + (mismatch && reads == 32));
}
static void reset(void)
{
    memset(&context, 0, sizeof(context));memset(hardware, 0, sizeof(hardware));
    context.hw = hardware; hardware[TSF_QUERY_HW_UP_OFFSET] = 1;
    memcpy(original_hardware, hardware, sizeof(hardware));
    reads = mismatch = down_after = 0;
}
int main(void)
{
    uint8_t output[52], before[52];
    struct shm_query_reply reply;
    unsigned int i;
    reset();memset(output,0xa5,sizeof(output));memcpy(before,output,sizeof(output));
    assert(shm_query(&context,0,48)==-14);
    for (int len=-1;len<48;++len) assert(shm_query(&context,(char*)output,len)==-14);
    assert(!reads && !memcmp(output,before,sizeof(output)));
    assert(shm_query(&context,(char*)output+1,48)==0);memcpy(&reply,output+1,48);
    assert(reads==32 && reply.magic==SHM_QUERY_MAGIC && reply.version==1 && reply.size==48 && reply.status==1);
    for(i=0;i<16;++i) assert(reply.words[i]==0xa100+i);
    assert(output[0]==0xa5 && output[49]==0xa5 && output[50]==0xa5 && output[51]==0xa5);
    assert(!memcmp(hardware,original_hardware,sizeof(hardware)));
    for (unsigned int scenario=0;scenario<6;++scenario) {
        reset();
        if(scenario==0) context.hw=0;
        if(scenario==1) hardware[TSF_QUERY_HW_UP_OFFSET]=0;
        if(scenario==2) mismatch=1;
        if(scenario==3) down_after=16;
        if(scenario==4) down_after=32;
        if(scenario==5) down_after=1;
        assert(shm_query(scenario==0?0:&context,(char*)output+1,48)==0);memcpy(&reply,output+1,48);
        assert(reply.status==0);
        for(i=0;i<16;++i) assert(reply.words[i]==0);
        assert(reads==(scenario<2?0:scenario==3?16:scenario==5?1:32));
        assert(output[0]==0xa5 && output[49]==0xa5);
    }
    puts("fixed SHMQ harness: all bounds/read-only invariants passed");
    return 0;
}
