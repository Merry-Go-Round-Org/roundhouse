import {test,expect} from "@playwright/test";
test("live chat composer is compact, accessible and responsive without sending suggestions",async({page})=>{
  const writes:string[]=[];
  await page.route("**/api/v1/**",route=>{
    const path=new URL(route.request().url()).pathname;
    if(route.request().method()==="POST")writes.push(path);
    if(path.endsWith("/auth/me"))return route.fulfill({json:{id:"operator",role:"developer"}});
    return route.fulfill({json:[]});
  });
  await page.setViewportSize({width:1440,height:1100});await page.goto("/"); await page.getByRole("button", {name:"Open agent chat",exact:true}).click();
  const chat=page.getByRole("complementary",{name:"Agent chat"});
  await expect(chat.getByRole("heading",{name:"How can I help?"})).toBeVisible();
  const input=chat.getByLabel("Message the agent",{exact:true});
  await expect(input).toHaveAttribute("placeholder",/Ask a question/);
  const prompt=chat.getByRole("button",{name:"Explain the workflow",exact:true});
  await prompt.focus();await page.keyboard.press("Enter");
  await expect(input).toHaveValue("Explain the model risk workflow and where I should start.");
  expect(writes).toEqual([]);
  await input.focus();await page.keyboard.press("End");await page.keyboard.press("Enter");
  expect(writes).toEqual([]);
  await expect(input).toHaveValue(/\n$/);
  await page.screenshot({path:"test-results/chat-desktop.png",fullPage:true});
  await page.setViewportSize({width:390,height:844});await chat.scrollIntoViewIfNeeded();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
  await expect(chat.getByRole("button",{name:"Send",exact:true})).toBeEnabled();
  await chat.screenshot({path:"test-results/chat-mobile.png"});
});
