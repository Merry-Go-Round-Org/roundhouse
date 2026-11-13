import {expect,it,vi} from "vitest";
import {readAuditEvents} from "./auditHistory";
import {createModelClient} from "./api";
const event={id:"e1",resource_id:"r1",action:"human_review_decision",created_at:"2026-11-07T10:00:00Z",payload:{decision:"accepted",conditions:["Recheck"]}};
it("keeps only public fields and preserves event labels without inferring approval",()=>{
  expect(readAuditEvents([{...event,actor_id:"private",payload:{...event.payload,prompt:"secret",reasoning:"hidden"}}])).toEqual([event]);
  expect(readAuditEvents([{...event,action:"tool_result",payload:{tool:"read_report",status:"ok",output:"private",decision:"accepted"}}])[0].payload).toEqual({tool:"read_report",status:"ok"});
});
it("rejects malformed, duplicate and overfull event pages",()=>{
  for(const value of [{},[event,event],[{...event,created_at:"bad"}],[{...event,payload:{conditions:"bad"}}],Array.from({length:21},(_,i)=>({...event,id:String(i)}))])expect(()=>readAuditEvents(value)).toThrow();
});
it("uses the encoded model endpoint, page limit and cookie session",async()=>{
  const transport=vi.fn<typeof fetch>(async()=>new Response(JSON.stringify([event])));
  const api=createModelClient("/api/v1",transport);
  expect(await api.listModelHistory("m1",20)).toEqual([event]);
  expect(transport.mock.calls[0][0]).toBe("/api/v1/models/m1/history?offset=20&limit=20");
  expect(transport.mock.calls[0][1]?.credentials).toBe("include");
  await expect(api.listModelHistory("m1",-1)).rejects.toThrow();expect(transport).toHaveBeenCalledTimes(1);
});
