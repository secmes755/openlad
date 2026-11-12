"""Home-page cleanup and browser-side session isolation contracts."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'frontend/web/dist'


def test_welcome_automatic_query_entries_are_removed():
    index = (WEB / 'index.html').read_text()
    app = (WEB / 'js/app.js').read_text()
    i18n = (WEB / 'js/i18n.js').read_text()
    css = (WEB / 'css/style.css').read_text()
    assert 'example-queries' not in index
    assert 'example-chip' not in index
    assert 'welcome.example' not in i18n
    assert 'sendExample' not in app
    assert '.example-queries' not in css
    assert '.example-chip' not in css


def test_session_functions_parse_and_ignore_stale_loads():
    app_path = WEB / 'js/app.js'
    subprocess.run(['node', '--check', str(app_path)], check=True, capture_output=True)
    source = app_path.read_text()
    functions = source[source.index('function createNewSession()'):
                         source.index('async function deleteSession')]
    send_message = source[source.index('async function sendMessage()'):
                           source.index('// ===== Query transport')]
    script = r'''
let currentSessionId = 'A';
let sessionMessages = [{role: 'user', content: 'A initial'}];
let sessionEpoch = 0;
let sessionLoadPending = false;
let isLoading = false;
let currentIndustry = 'auto';
let rendered = '';
let queryCalls = 0;
let queryHandler = async () => ({answer: 'answer'});
function apiFetch(path) {
    const id = path.includes('/A/') ? 'A' : 'B';
    const delay = id === 'A' ? 5 : 30;
    return new Promise(resolve => setTimeout(() => resolve({
        ok: true,
        json: async () => ({messages: [{role: 'user', content: id + ' message'}]})
    }), delay));
}
async function loadSessions() {}
function showWelcome() { sessionMessages = []; rendered = ''; }
function appendMessageToDOM(role, content) { rendered = content; }
function appendLoadingMessage() { return 'loading'; }
function removeLoadingMessage() {}
function scrollToBottom() {}
function __(key) { return key; }
async function runQuery(payload) { queryCalls++; return await queryHandler(payload); }
const window = {history: {replaceState() {}}};
const elements = {};
const document = {getElementById(id) {
    if (!elements[id]) elements[id] = {style: {}, innerHTML: '', value: '', disabled: false};
    return elements[id];
}};
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
''' + functions + send_message + r'''
(async () => {
    const first = switchSession('B');
    const second = switchSession('A');
    await Promise.all([first, second]);
    if (currentSessionId !== 'A' || sessionMessages[0].content !== 'A message'
            || rendered !== 'A message') {
        throw new Error('stale session response replaced the selected session');
    }

    const loading = switchSession('B');
    createNewSession();
    await loading;
    if (currentSessionId !== null || sessionMessages.length !== 0 || rendered !== '') {
        throw new Error('in-flight load populated a newly created conversation');
    }

    currentSessionId = 'A';
    sessionLoadPending = true;
    queryCalls = 0;
    document.getElementById('messageInput').value = 'must wait';
    await sendMessage();
    if (queryCalls !== 0) throw new Error('message sent while session history was loading');

    sessionLoadPending = false;
    sessionMessages = [];
    rendered = '';
    queryHandler = async () => {
        await sleep(30);
        return {answer: 'stale answer', sources: []};
    };
    document.getElementById('messageInput').value = 'old query';
    const oldQuery = sendMessage();
    await sleep(5);
    createNewSession();
    await oldQuery;
    if (sessionMessages.length !== 0 || rendered !== '') {
        throw new Error('stale query result populated a new conversation');
    }

    currentSessionId = 'A';
    sessionMessages = [];
    rendered = '';
    queryHandler = async () => { await sleep(30); throw new Error('stale failure'); };
    document.getElementById('messageInput').value = 'old failing query';
    const oldFailure = sendMessage();
    await sleep(5);
    createNewSession();
    await oldFailure;
    if (sessionMessages.length !== 0 || rendered !== '') {
        throw new Error('stale query error populated a new conversation');
    }

    currentSessionId = 'A';
    sessionMessages = [{role: 'user', content: 'context'}];
    queryCalls = 0;
    queryHandler = async () => ({answer: 'current answer', sources: []});
    document.getElementById('messageInput').value = 'current query';
    await sendMessage();
    if (queryCalls !== 1 || sessionMessages.at(-1).content !== 'current answer') {
        throw new Error('current session query was blocked or not recorded: ' + JSON.stringify({queryCalls, sessionMessages, isLoading, sessionLoadPending, currentSessionId, rendered}));
    }
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run(['node', '-e', script], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
