#include "beacon_stamp.h"
#include "tsf_query.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static _Alignas(struct wlc) uint8_t context_bytes[0x700];
static struct wlc *context=(struct wlc*)context_bytes;
static uint8_t hardware[0xc0], storage[1024], native_storage[1024];
static struct packet packet, native_packet;
static void *scb=(void*)(uintptr_t)0x1234;
static unsigned int prefix,calls,zero_after,short_after,short_descriptor,already,misplaced_after;
static int expected_preamble;static void *expected_key;static uint16_t *expected_offset;
void *fw_injection_scb(struct wlc *wlc) { assert(wlc==context);return scb; }
int fw_descriptor_build(struct wlc *wlc,struct packet *p,void *station,int preamble,
 unsigned int frag,unsigned int nfrag,unsigned int fifo,int next,void *key,unsigned int rate,uint16_t *offset)
{
 assert(wlc==context&&p==&packet&&station==scb&&preamble==expected_preamble);
 assert(!frag&&nfrag==1&&fifo==1&&!next&&key==expected_key&&rate==48&&offset==expected_offset);
 ++calls;p->data-=124+prefix;p->len+=124+prefix;p->tag_flags|=0x80;
 memset(p->data,0x55,124+prefix);
 if(prefix)p->data[0]=prefix==16?1:0;
 p->data[prefix+2]=short_descriptor?1:0;p->data[prefix+3]=0x4a;
 p->data[prefix+4]=already?0x82:2;p->data[prefix+5]=0x12;p->data[prefix+100]=2;
 if(zero_after){p->data=storage+sizeof(storage);p->len=0;}
 if(short_after)p->len=10;
 if(misplaced_after)p->data-=4;
 memcpy(native_storage,storage,sizeof(storage));native_packet=*p;return 0x1abc;
}
static void reset(unsigned int tso,unsigned int kind)
{
 memset(context_bytes,0,sizeof(context_bytes));memset(hardware,0,sizeof(hardware));
 memset(storage,0xcc,sizeof(storage));memset(&packet,0,sizeof(packet));
 beacon_stamp_stats=(struct beacon_stamp_stats){BEACON_STAMP_MAGIC,1,sizeof(beacon_stamp_stats),1,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0};
 context->monitor=1;context->hw=hardware;hardware[TSF_QUERY_HW_UP_OFFSET]=1;
 prefix=tso;context_bytes[0x674]=!tso;calls=zero_after=short_after=short_descriptor=already=misplaced_after=0;
 expected_preamble=0;expected_key=0;expected_offset=0;
 packet.data=storage+512;packet.len=40;packet.tag_flags=0x8004;
 memset(packet.data,0,40);packet.data[0]=(uint8_t)kind;
 memset(packet.data+4,0xff,6);packet.data[10]=packet.data[16]=2;
 packet.data[32]=100;packet.data[37]=2;
}
static void invoke(int should_stamp)
{
 unsigned int i;assert(beacon_stamp_build(context,&packet,scb,expected_preamble,0,1,1,0,expected_key,48,expected_offset)==0x1abc);
 assert(calls==1&&!memcmp(&packet,&native_packet,sizeof(packet)));
 if(should_stamp)native_storage[512-124+4]|=0x80;
 for(i=0;i<sizeof(storage);++i)assert(storage[i]==native_storage[i]);
 assert(!(beacon_stamp_stats.sequence&1));
}
int main(void)
{
 uint8_t output[88],before[88];unsigned int i;
 for(unsigned int mode=0;mode<3;++mode){reset(mode==0?0:mode==1?4:16,0x80);invoke(1);
  assert(beacon_stamp_stats.stamped==1&&beacon_stamp_stats.last_valid);
  assert(beacon_stamp_stats.last_low==0x4a00&&beacon_stamp_stats.last_high_before==0x1202&&beacon_stamp_stats.last_high_after==0x1282);
  assert(beacon_stamp_stats.last_bss_index==2&&beacon_stamp_stats.last_prefix==prefix);}
 reset(4,0x80);already=1;invoke(0);assert(beacon_stamp_stats.already_stamped==1&&!beacon_stamp_stats.stamped);
 for(unsigned int kind=0;kind<3;++kind){reset(4,kind==0?0x50:kind==1?0x08:0x10);invoke(0);assert(!beacon_stamp_stats.beacon_candidates);}
 for(unsigned int scenario=0;scenario<10;++scenario){reset(4,0x80);
  if(scenario==0)zero_after=1;if(scenario==1)short_after=1;if(scenario==2)short_descriptor=1;
  if(scenario==3)packet.data[1]=0x40;if(scenario==4)packet.data[4]=2;if(scenario==5)packet.data[22]=1;
  if(scenario==6)packet.data[37]=3;if(scenario==7)expected_key=scb;if(scenario==8)expected_preamble=7;if(scenario==9)misplaced_after=1;
  invoke(0);assert(!beacon_stamp_stats.stamped);if(scenario<3||scenario==9)assert(beacon_stamp_stats.rejected_headers==1);}
 memset(output,0xa5,sizeof(output));memcpy(before,output,sizeof(output));
 assert(beacon_stamp_query(0,80)==-14&&beacon_stamp_query((char*)output,79)==-14&&!memcmp(output,before,sizeof(output)));
 beacon_stamp_stats.sequence=1;assert(beacon_stamp_query((char*)output,88)==-16&&!memcmp(output,before,sizeof(output)));
 beacon_stamp_stats.sequence=2;assert(!beacon_stamp_query((char*)output+1,80));assert(output[0]==0xa5);
 for(i=81;i<sizeof(output);++i)assert(output[i]==0xa5);
 puts("beacon stamp: all forwarding/bounds/single-write invariants passed");return 0;
}
