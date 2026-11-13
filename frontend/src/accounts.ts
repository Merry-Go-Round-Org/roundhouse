import type { AuthRole } from "./auth";
export type Account = {id:string;username:string;role:AuthRole;active:boolean};
export const accountRoles: AuthRole[] = ["admin","developer","validator","approver"];
export function readAccount(value:unknown): Account {
  const r=value as Account;
  if(!r||typeof r!=="object"||typeof r.id!=="string"||!r.id.trim()||typeof r.username!=="string"||!r.username.trim()||
    !accountRoles.includes(r.role)||typeof r.active!=="boolean")throw new Error("Invalid public account response.");
  return {id:r.id,username:r.username,role:r.role,active:r.active};
}
export function readAccounts(value:unknown):Account[]{
  if(!Array.isArray(value))throw new Error("Invalid account list.");
  const rows=value.map(readAccount);
  if(new Set(rows.map(row=>row.id)).size!==rows.length)throw new Error("Duplicate account IDs.");
  return rows;
}
