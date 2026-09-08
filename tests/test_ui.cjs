// Browser-only fixture: all APIs are intercepted; no user files or backend mutations.
const {chromium} = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'));
  const server = http.createServer((req, res) => {
    if (['/support/donate-wechat.png', '/support/donate-alipay.png'].includes(req.url)) {
      res.setHeader('Content-Type', 'image/png');
      res.end(fs.readFileSync(path.join(__dirname, '..', 'static', req.url)));
    } else { res.setHeader('Content-Type', 'text/html; charset=utf-8'); res.end(html); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({channel:'msedge', headless:true});
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let preview, executed, scanIds, stateCalls = 0, failSpace = false;
    const buckets = [
      {id:'ai-a',tool:'ai',category:'assistant',name:'AI cache A',risk:'safe',size:100},
      {id:'ai-b',tool:'ai',category:'assistant',name:'AI cache B',risk:'safe',size:200},
      {id:'dev-a',tool:'dev',category:'dev',name:'Dev cache',risk:'safe',size:500}
    ];
    const tools = [{id:'ai',name:'AI fixture',category:'assistant'}, {id:'dev',name:'Dev fixture',category:'dev'}];
    await page.route('**/api/**', async route => {
      const request = route.request();
      assert.equal(request.headers()['x-deepclean-token'], 'fixture-token');
      const endpoint = new URL(request.url()).pathname;
      let data;
      if (endpoint === '/api/state') {
        stateCalls++;
        if (failSpace) { await route.fulfill({status:503,contentType:'application/json',body:'{"error":"fixture unavailable"}'}); return; }
        data = {admin:false,win:true,drive_letter:'C',drive:{free:1024,total:10000,used:8976},tools,buckets,support_links:[]};
      } else if (endpoint === '/api/scan/start') {
        scanIds = request.postDataJSON().ids; data = {ok:true};
      } else if (endpoint === '/api/progress') {
        data = {status:'done',per:Object.fromEntries(buckets.map(b => [b.id,{size:b.size,status:'done'}]))};
      } else if (endpoint === '/api/roots') data = {ok:true,categories:{}};
      else if (endpoint === '/api/clean/preview') {
        preview = request.postDataJSON();
        data = {ok:true,plan_token:'approved-fixture',per:{'ai-b':{size:200,count:1}}};
      } else if (endpoint === '/api/clean') {
        executed = request.postDataJSON(); data = {ok:true};
      } else if (endpoint === '/api/clean/progress') data = {status:'done',total_freed:200,total_skipped:0,via:'recycle',per:{'ai-b':{freed:200}},report:{}};
      else data = {ok:true,items:[]};
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/#token=fixture-token`);
    await page.waitForFunction(() => S.buckets.length === 3 && S.scanned);
    await page.locator('#btnScan').waitFor(); // init finishes by rendering Home
    assert.equal(new URL(page.url()).hash, '');
    await page.evaluate(() => { setView('ai'); });
    await page.locator('[data-bucket="ai-a"]').click();
    assert.equal(await page.locator('[data-bucket="ai-a"]').getAttribute('aria-pressed'), 'false');
    await page.locator('#btnClean').click();
    await page.locator('#maskConfirm.show').waitFor();
    assert.deepEqual(preview.ids, ['ai-b']);
    assert.equal(await page.locator('#confirmTotal').textContent(), '200 B');
    await page.locator('#btnConfirmOk').click();
    await page.locator('#maskSuccess.show').waitFor();
    assert.deepEqual(executed, {plan_token:'approved-fixture',dry:false});
    await page.waitForFunction(() => document.querySelector('#successLine').textContent.includes('→'));
    assert.match(await page.locator('#successLine').textContent(), /1\.0 KB → 1\.0 KB/);
    assert.equal(await page.locator('#donateBox').isVisible(), false);
    assert.equal(await page.locator('.pay-slot').count(), 0);
    await page.locator('#btnSuccessClose').click();
    await page.locator('#btnClean').click();
    await page.locator('#maskConfirm.show').waitFor();
    failSpace = true;
    await page.locator('#btnConfirmOk').click();
    await page.locator('#maskSuccess.show').waitFor();
    await page.waitForFunction(() => document.querySelector('#successLine').textContent.includes('读取失败'));
    assert.equal(await page.locator('#successFreed').textContent(), '200 B');
    const configuredSupport = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'support.json'), 'utf8')).links;
    await page.evaluate(links => { supportLinks = links; renderSupport(); }, configuredSupport);
    assert.equal(await page.locator('#donateBox .pay-slot').count(), 3);
    assert.equal(await page.locator('#donateBox .pay-qr').count(), 2);
    await page.locator('#donateBox .pay-qr summary').first().click();
    await page.waitForFunction(() => document.querySelector('#donateBox img').naturalWidth > 0);
    assert.match(await page.locator('#donateBox .support-note').textContent(), /免费使用/);
    failSpace = false;
    await page.locator('#btnSuccessClose').click();
    await page.locator('[data-toolscan="ai"]').click();
    await page.waitForFunction(() => !scanning);
    assert.deepEqual(scanIds, ['ai-a','ai-b']);
    await page.locator('#btnSettings').click();
    await page.locator('#btnQuit').click();
    await page.getByText('DeepClean 已退出，可以关闭此页面。').waitFor();
    assert.deepEqual(errors, []);
    console.log('UI checks passed: token bootstrap, individual selection, visible scope, approved plan, actual disk values, hidden unconfigured support; no browser script errors.');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
