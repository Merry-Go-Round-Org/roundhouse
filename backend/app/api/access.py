"""Minimal backend sign-in screen for Swagger/shared demos; React owns its full login UX."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    return HTMLResponse(
        """<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Model Risk Factory · Sign in</title>
<style>body{font:18px system-ui;background:#f3f5f7;color:#143144;max-width:420px;margin:12vh auto;padding:24px}input,button{box-sizing:border-box;width:100%;padding:12px;margin:8px 0 20px;font:inherit}button{background:#143144;color:white;border:0;border-radius:6px}small{display:block;color:#526470}a{color:#123e88}</style>
<h1>Model Risk Factory</h1><p>Sign in with the account shared privately by your backend teammate.</p><form id="login"><label>Username<input name="username" required autocomplete="username" maxlength="100"></label><label>Password<input type="password" name="password" required autocomplete="current-password" maxlength="256"></label><button>Sign in</button></form><p id="status" role="status"></p><nav hidden id="links"><a href="/docs">Open API explorer</a></nav><small>Independent reviewers use their own accounts. This is a prototype workspace.</small>
<script>document.querySelector('form').addEventListener('submit',async e=>{e.preventDefault();const status=document.querySelector('#status'),button=document.querySelector('button');button.disabled=true;status.textContent='Signing in…';try{const form=new FormData(e.target),r=await fetch('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(Object.fromEntries(form))});const data=await r.json();status.textContent=r.ok?'Signed in as '+data.role:typeof data.detail==='string'?data.detail:'Sign-in failed';document.querySelector('#links').hidden=!r.ok;if(r.ok)e.target.reset()}catch{status.textContent='Could not reach API; retry.'}finally{button.disabled=false}})</script></html>""",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
