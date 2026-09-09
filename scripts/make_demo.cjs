// Record clearly labelled sample data. All API calls are mocked; no disk scan.
const {chromium} = require('playwright');
const http = require('node:http'), fs = require('node:fs'), path = require('node:path');
const root = path.resolve(__dirname, '..'), out = path.join(root, 'docs', 'promotion', 'assets');
fs.mkdirSync(out, {recursive:true});
const support = JSON.parse(fs.readFileSync(path.join(root, 'support.json'))).links;
const GB = 1024 ** 3;
const tools = [{id:'demo-ai',name:'AI 助手（演示）',category:'assistant'}, {id:'demo-model',name:'本地模型（演示）',category:'model'}];
const buckets = [
  {id:'demo-cache',tool:'demo-ai',category:'assistant',name:'可重建缓存',risk:'safe',size:1.2*GB,scanned:true,desc:'演示数据，不读取真实用户目录'},
  {id:'demo-history',tool:'demo-ai',category:'assistant',name:'会话与历史',risk:'danger',locked:true,size:0.3*GB,scanned:true,desc:'锁定保护，不参与清理'},
  {id:'demo-model',tool:'demo-model',category:'model',name:'模型文件',risk:'migrate',size:12*GB,scanned:true,desc:'模型仅统计，不删除；迁移目前仅预览'}
];
const server = http.createServer((req,res) => {
  const asset = ['/support/donate-wechat.png','/support/donate-alipay.png'].includes(req.url);
  res.setHeader('Content-Type',asset ? 'image/png' : 'text/html; charset=utf-8');
  res.end(fs.readFileSync(path.join(root,'static',asset ? req.url : 'index.html')));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser = await chromium.launch({channel:'msedge',headless:true});
 const context = await browser.newContext({viewport:{width:1280,height:900},recordVideo:{dir:out,size:{width:1280,height:900}}});
 const page = await context.newPage();
 try {
  await page.route('**/api/**', async route=>{
   const p = new URL(route.request().url()).pathname;
   let data = {ok:true};
   if(p==='/api/state') data={admin:false,win:true,drive_letter:'C',drive:{free:24*GB,total:256*GB,used:232*GB},tools,buckets,support_links:support};
   if(p==='/api/progress') data={status:'done',partial:false,per:Object.fromEntries(buckets.map(b=>[b.id,{size:b.size,status:'done'}]))};
   if(p==='/api/roots') data={ok:true,categories:{}};
   if(p==='/api/history') data={ok:true,items:[]};
   if(p==='/api/clean/preview') data={ok:true,plan_token:'demo-only',per:{'demo-cache':{size:1.2*GB,count:128}}};
   await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/#token=demo-only`);
  await page.locator('#btnScan').waitFor();
  await page.evaluate(()=>{
   const note=document.createElement('div'); note.textContent='演示数据 · 不读取真实目录 · 免费使用＋自愿支持作者';
   note.style.cssText='position:fixed;top:8px;right:12px;z-index:99999;text-align:center;padding:8px;background:#172b25;color:#ccf7dd;font-size:13px;pointer-events:none';document.body.append(note);
  });
  await page.screenshot({path:path.join(out,'01-home.png')});
  await page.waitForTimeout(6500);
  await page.evaluate(()=>setView('ai'));
  await page.waitForTimeout(1200);
  await page.screenshot({path:path.join(out,'02-results.png')});
  await page.waitForTimeout(8000);
  await page.locator('#btnClean').click();
  await page.locator('#maskConfirm.show').waitFor();
  await page.screenshot({path:path.join(out,'03-confirm.png')});
  await page.waitForTimeout(7000);
  await page.locator('#btnConfirmCancel').click();
  await page.locator('#btnSettings').click();
  await page.locator('#maskSettings.show').waitFor();
  await page.waitForTimeout(500);
  await page.locator('#maskSettings .pay-qr summary').first().click();
  await page.waitForFunction(()=>document.querySelector('#maskSettings .pay-qr img').naturalWidth>0);
  await page.screenshot({path:path.join(out,'04-support.png')});
  await page.waitForTimeout(8500);
 } finally {
  await context.close();
  await page.video().saveAs(path.join(out,'deepclean-demo.webm'));
  await page.video().delete();
  await browser.close();
  await new Promise(resolve=>server.close(resolve));
 }
 console.log('Demo screenshots and video: '+out);
})().catch(e=>{console.error(e);process.exitCode=1;server.close();});
