// Real Chromium editing regression; uses an isolated profile and no network.
const {app, BrowserWindow} = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {readComposerText, insertComposerText} = require('../../electron/saved-send.cjs');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'saved-send-editor-'));
app.setPath('userData', profile);
app.whenReady().then(async () => {
  let win;
  try {
    win = new BrowserWindow({show: false, webPreferences: {sandbox: true, contextIsolation: true}});
    await win.loadURL('data:text/html,' + encodeURIComponent('<style>#editor {white-space:break-spaces} #editor p {white-space:pre-wrap} #editor p.placeholder {white-space:nowrap}</style><div id="editor" contenteditable="true"></div>'));
    const cases = ['First line  \nSecond line', '  leading  and  repeated spaces\n\nLast',
      'A\tB', 'A\u00a0B', '**literal**\n- list\n<tags> & emoji 🎉', 'Last line  ',
      'First paragraph with trailing spaces  \nSecond paragraph with trailing spaces  \nThird paragraph'];
    for (const text of cases) {
      for (const style of ['', 'white-space: normal !important', 'white-space: pre-wrap']) {
        const result = await win.webContents.executeJavaScript(`(() => {
          const e = document.querySelector('#editor');
          e.innerHTML = '<p dir="auto" data-empty-paragraph="true" class="placeholder"><br class="ProseMirror-trailingBreak"></p>';
          e.setAttribute('style', ${JSON.stringify(style)});
          const before = [e.style.getPropertyValue('white-space'), e.style.getPropertyPriority('white-space')];
          const inserted = (${insertComposerText.toString()})(e, ${JSON.stringify(text)});
          return {inserted, text: (${readComposerText.toString()})(e), before,
            after: [e.style.getPropertyValue('white-space'), e.style.getPropertyPriority('white-space')]};
        })()`);
        assert(result.inserted);
        assert.equal(result.text, text);
        assert.deepEqual(result.after, result.before);
      }
    }
    // Replacing a previously rejected NBSP draft must not append another copy.
    const repaired = await win.webContents.executeJavaScript(`(() => {
      const e = document.querySelector('#editor'); e.innerHTML = 'First&nbsp;&nbsp;<div>Second</div>';
      (${insertComposerText.toString()})(e, ${JSON.stringify('First  \nSecond')});
      return (${readComposerText.toString()})(e);
    })()`);
    assert.equal(repaired, 'First  \nSecond');
    console.log('Chromium placeholder regression passed: 21 insertions and existing draft repair');
  } catch (error) {
    console.error(error); process.exitCode = 1;
  } finally {
    if (win) win.destroy();
    app.exit(process.exitCode || 0);
  }
});
app.on('quit', () => fs.rmSync(profile, {recursive: true, force: true}));
