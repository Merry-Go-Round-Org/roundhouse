import {expect,it,vi} from "vitest";
import {readAccount,readAccounts} from "./accounts";
import {createModelClient} from "./api";
const account={id:"u1",username:"reviewer",role:"validator" as const,active:true};
it("retains only public account fields and rejects invalid lists",()=>{
  expect(readAccount({...account,password_hash:"must not retain"})).toEqual(account);
  expect(()=>readAccounts([account,account])).toThrow();
  expect(()=>readAccount({...account,active:"yes"})).toThrow();
});
it("blocks self disable and stale identity before any write",async()=>{
  const transport=vi.fn<typeof fetch>(async()=>new Response(JSON.stringify([{...account,role:"admin"}])));
  const api=createModelClient("/api/v1",transport);
  await expect(api.disableAccount(account,"u1")).rejects.toThrow();expect(transport).not.toHaveBeenCalled();
  await expect(api.disableAccount(account,"admin1")).rejects.toThrow("changed");expect(transport).toHaveBeenCalledTimes(1);
});
it("uses one cookie-authenticated disable POST after a fresh read",async()=>{
  const transport=vi.fn<typeof fetch>(async(_url,init)=>new Response(JSON.stringify(init?.method==="POST"?{...account,active:false}:[account])));
  expect((await createModelClient("/api/v1",transport).disableAccount(account,"admin1")).active).toBe(false);
  expect(transport.mock.calls[1][0]).toBe("/api/v1/auth/users/u1/disable");
  expect(transport.mock.calls[1][1]?.credentials).toBe("include");
});
it("validates account creation before sending and never retries uncertain creation",async()=>{
  const transport=vi.fn<typeof fetch>(async()=>new Response(JSON.stringify({detail:"Unavailable"}),{status:503}));
  const api=createModelClient("/api/v1",transport);
  await expect(api.createAccount("new","short","developer")).rejects.toThrow();expect(transport).not.toHaveBeenCalled();
  await expect(api.createAccount("new","fixture-password-only","developer")).rejects.toThrow();expect(transport).toHaveBeenCalledTimes(1);
});
