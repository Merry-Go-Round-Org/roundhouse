import { expect,it,vi } from "vitest";
import { readFinding,findingPatch } from "./findings";
import { createModelClient } from "./api";
const finding = {id:"f1",model_id:"m1",report_id:"r1",check_id:"fairness",severity:"review",status:"open" as const,summary:"Review fairness",comments:[],owner_id:null,due_date:null};
it("requires a comment and only patches changed fields with explicit UTC",()=>{
  expect(findingPatch(finding,"open","","","Noted")).toEqual({comment:"Noted"});
  expect(findingPatch(finding,"in_progress","u1","2026-11-08T10:30","Assign")).toEqual({comment:"Assign",status:"in_progress",owner_id:"u1",due_date:"2026-11-08T10:30:00.000Z"});
  expect(()=>findingPatch(finding,"open","",""," ")).toThrow();
  expect(()=>findingPatch(finding,"open","","2026-02-30T10:30","bad date")).toThrow();
});
it("does not truncate unchanged due-date seconds and supports explicit clearing",()=>{
  const withDue={...finding,owner_id:"u1",due_date:"2026-11-08T10:30:15Z"};
  expect(findingPatch(withDue,"open","u1","2026-11-08T10:30","Keep")).toEqual({comment:"Keep"});
  expect(findingPatch(withDue,"open","","","Clear")).toEqual({comment:"Clear",owner_id:null,due_date:null});
});
it("rejects another model and malformed finding evidence",()=>{
  expect(()=>readFinding(finding,"other")).toThrow();
  expect(()=>readFinding({...finding,comments:[{comment:"bad"}]},"m1")).toThrow();
});
it("detects stale findings before writing and never retries failed patches",async()=>{
  for(const status of [403,409,503]) {
    const transport=vi.fn<typeof fetch>(async(_url,init)=>new Response(JSON.stringify(init?.method==="PATCH"?{detail:"Denied or uncertain"}:[finding]),{status:init?.method==="PATCH"?status:200}));
    await expect(createModelClient("/api/v1",transport).updateFinding(finding,{comment:"Note"},0)).rejects.toThrow();
    expect(transport.mock.calls.filter(([,init])=>init?.method==="PATCH")).toHaveLength(1);
  }
  const transport=vi.fn<typeof fetch>(async()=>new Response(JSON.stringify([{...finding,status:"resolved"}])));
  await expect(createModelClient("/api/v1",transport).updateFinding(finding,{comment:"Note"},0)).rejects.toThrow("changed");
  expect(transport).toHaveBeenCalledTimes(1);
});
