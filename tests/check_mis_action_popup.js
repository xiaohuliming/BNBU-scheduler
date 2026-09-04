// Run with playwright-cli in an isolated persistent Chromium profile containing
// only the unpacked release ZIP. Start on about:blank, not a live MIS page.
// playwright-cli run-code --filename tests/check_mis_action_popup.js
async (page) => {
  if (page.url() !== 'about:blank') throw new Error('Use an isolated about:blank test window');
  const worker = page.context().serviceWorkers().find(item => item.url().endsWith('/background.js'));
  if (!worker) throw new Error('Load the unpacked release extension in the isolated profile');
  const client = await page.context().newCDPSession(page);
  let { targetInfos } = await client.send('Target.getTargets');
  let target = targetInfos.find(info => info.url.endsWith('/popup.html'));
  if (!target) {
    await page.bringToFront();
    await worker.evaluate(async () => {
      const win = await chrome.windows.getLastFocused();
      await chrome.action.openPopup({ windowId: win.id });
    });
    ({ targetInfos } = await client.send('Target.getTargets'));
    target = targetInfos.find(info => info.url.endsWith('/popup.html'));
  }
  if (!target) throw new Error('Actual action popup did not open');
  const { sessionId } = await client.send('Target.attachToTarget', { targetId: target.targetId, flatten: false });
  try {
    const pending = new Promise((resolve, reject) => {
      client.on('Target.receivedMessageFromTarget', event => {
        if (event.sessionId !== sessionId) return;
        const reply = JSON.parse(event.message);
        if (reply.id !== 1) return;
        if (reply.error) reject(new Error(JSON.stringify(reply.error)));
        else resolve(reply.result);
      });
    });
    await client.send('Target.sendMessageToTarget', {
      sessionId,
      message: JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: {
        expression: `JSON.stringify({width:innerWidth,body:document.body.getBoundingClientRect().width,titleWidth:document.querySelector('.brand-copy').getBoundingClientRect().width,version:chrome.runtime.getManifest().version})`,
        returnByValue: true
      } })
    });
    const response = await pending;
    if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
    const measured = JSON.parse(response.result.value);
    if (measured.width !== 408 || measured.body !== 408 || measured.titleWidth < 180) {
      throw new Error('Action popup collapsed: ' + JSON.stringify(measured));
    }
    return measured;
  } finally {
    await client.send('Target.detachFromTarget', { sessionId });
    await client.detach();
  }
}
