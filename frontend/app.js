// ==========================================================================
// Precis — 會議紀錄生成器
// ==========================================================================

// --- Config（從 config.json 載入，gitignored） ---
let CONFIG = { API_URL: '', COGNITO_CLIENT_ID: '', COGNITO_REGION: '' };

async function loadConfig() {
    try {
        const res = await fetch('config.json');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        CONFIG = await res.json();
        return;
    } catch { /* fall through */ }
    if (window.PRECIS_CONFIG) {
        CONFIG = window.PRECIS_CONFIG;
        return;
    }
    console.error('Failed to load config — API calls will not work');
}

// --- Auth（直接 call Cognito API，自訂 login UI） ---
let currentUser = null;

const COGNITO_URL = () => `https://cognito-idp.${CONFIG.COGNITO_REGION || 'ap-east-1'}.amazonaws.com/`;

async function cognitoCall(action, payload) {
    const res = await fetch(COGNITO_URL(), {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-amz-json-1.1',
            'X-Amz-Target': `AWSCognitoIdentityProviderService.${action}`,
        },
        body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
        const msg = data.message || data.__type || 'Unknown error';
        throw { code: data.__type || '', message: msg };
    }
    return data;
}

// Pending challenge state for NEW_PASSWORD_REQUIRED
let pendingChallenge = null;

async function signIn(email, password) {
    const data = await cognitoCall('InitiateAuth', {
        AuthFlow: 'USER_PASSWORD_AUTH',
        ClientId: CONFIG.COGNITO_CLIENT_ID,
        AuthParameters: { USERNAME: email, PASSWORD: password },
    });

    // Handle NEW_PASSWORD_REQUIRED challenge (first login)
    if (data.ChallengeName === 'NEW_PASSWORD_REQUIRED') {
        pendingChallenge = { session: data.Session, email };
        return { challenge: 'NEW_PASSWORD_REQUIRED' };
    }

    return _completeAuth(data, email);
}

async function completeNewPassword(newPassword) {
    if (!pendingChallenge) throw { message: 'No pending challenge' };
    const email = pendingChallenge.email;
    const data = await cognitoCall('RespondToAuthChallenge', {
        ChallengeName: 'NEW_PASSWORD_REQUIRED',
        ClientId: CONFIG.COGNITO_CLIENT_ID,
        Session: pendingChallenge.session,
        ChallengeResponses: {
            USERNAME: email,
            NEW_PASSWORD: newPassword,
        },
    });
    pendingChallenge = null;
    return _completeAuth(data, email);
}

async function forgotPassword(email) {
    await cognitoCall('ForgotPassword', {
        ClientId: CONFIG.COGNITO_CLIENT_ID,
        Username: email,
    });
}

async function confirmForgotPassword(email, code, newPassword) {
    await cognitoCall('ConfirmForgotPassword', {
        ClientId: CONFIG.COGNITO_CLIENT_ID,
        Username: email,
        ConfirmationCode: code,
        Password: newPassword,
    });
}

async function changePassword(oldPassword, newPassword) {
    if (!currentUser?.accessToken) throw { message: 'Not signed in' };
    await cognitoCall('ChangePassword', {
        AccessToken: currentUser.accessToken,
        PreviousPassword: oldPassword,
        ProposedPassword: newPassword,
    });
}

function _completeAuth(data, email) {
    const idToken = data.AuthenticationResult.IdToken;
    const accessToken = data.AuthenticationResult.AccessToken;
    const b64 = idToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    const payload = JSON.parse(atob(b64));
    const user = {
        email: payload.email || email,
        name: payload.name || payload.email || email,
        token: idToken,
        accessToken: accessToken,
        expires_at: Date.now() + (data.AuthenticationResult.ExpiresIn || 3600) * 1000,
    };
    sessionStorage.setItem('precis-auth', JSON.stringify(user));
    return user;
}


function loadAuthFromSession() {
    try {
        const stored = sessionStorage.getItem('precis-auth');
        if (!stored) return null;
        const user = JSON.parse(stored);
        if (user.expires_at && Date.now() > user.expires_at) {
            sessionStorage.removeItem('precis-auth');
            return null;
        }
        return user;
    } catch {
        return null;
    }
}

function authHeaders() {
    return {
        'Content-Type': 'application/json',
        'Authorization': currentUser?.token || '',
    };
}

// Wrapper for API fetch calls — auto-logout on 401 (expired token)
async function apiFetch(url, options = {}) {
    const res = await fetch(url, options);
    if (res.status === 401) {
        logout();
        throw new Error('Session expired — please sign in again');
    }
    return res;
}

function logout() {
    sessionStorage.removeItem('precis-auth');
    currentUser = null;
    showLogin();
}

function showApp() {
    document.getElementById('login-screen').hidden = true;
    document.getElementById('app-layout').hidden = false;

    // 顯示用戶資料
    const name = currentUser.name || currentUser.email;
    document.getElementById('user-name').textContent = name;
    document.getElementById('user-avatar').textContent = (name[0] || 'U').toUpperCase();

    // 自動填 email
    const emailInput = document.getElementById('email');
    if (emailInput && !emailInput.value) {
        emailInput.value = currentUser.email;
    }

    // Settings 頁面顯示 email
    document.getElementById('settings-email').textContent = currentUser.email;
}

function showLogin() {
    document.getElementById('login-screen').hidden = false;
    document.getElementById('app-layout').hidden = true;
    _showAuthForm('auth-login');
}

function _showAuthForm(formId) {
    ['auth-login', 'auth-new-password', 'auth-forgot', 'auth-reset'].forEach(id => {
        document.getElementById(id).hidden = (id !== formId);
    });
}

// --- i18n 字串表 ---
const I18N = {
    zh: {
        logoSub: '精記',
        loginSub: '會議錄音 → 結構化會議紀錄',
        btnLogin: '登入 / 註冊',
        navNew: '新錄音',
        navHistory: '歷史記錄',
        navSettings: '設定',
        btnLogout: '登出',
        pageNew: '新錄音',
        pageHistory: '歷史記錄',
        pageSettings: '設定',
        step1: '設定要求',
        step2: '上傳錄音',
        step3: '完成',
        requirementsTitle: '設定要求',
        emailLabel: 'Email 地址 <span class="required">*</span>',
        emailHint: '完成後會 email 通知你',
        outputLangLabel: '輸出語言',
        summaryLenLabel: '摘要長度',
        lenShort: '簡短 (~300字)',
        lenMedium: '標準 (~800字)',
        lenDetailed: '詳細 (~1500字)',
        formatLabel: '輸出格式',
        fmtMinutes: '會議紀錄',
        fmtActions: '行動項目',
        fmtBoth: '兩者都要',
        customLabel: '額外要求',
        optional: '(選填)',
        btnNext: '下一步：上傳錄音',
        uploadTitle: '上傳會議錄影',
        dropLabel: '拖放錄音檔案到呢度',
        or: '或者',
        browse: '選擇檔案',
        uploadHint: '支援 MP4, M4A, WAV, MKV（最大 2GB）',
        btnBack: '返回修改要求',
        doneTitle: '已提交！',
        doneText: '你嘅會議錄影已經開始處理。',
        doneEmail: '完成後會 email 通知你：',
        doneEstimate: '預計處理時間：15–30 分鐘',
        btnNewJob: '提交另一個錄影',
        historyEmpty: '暫時冇記錄',
        historyEmptyText: '提交你嘅第一個會議錄音。',
        settingsTitle: '設定',
        settingsText: '登入後可以管理帳號設定。',
        settingsAccount: '帳號',
        customPlaceholder: '例如：重點列出技術決策、忽略閒聊部分、特別關注 timeline...',
        errEmail: '請填寫 email 地址',
        errNoApi: 'API URL 未設定，請部署 config.json',
        errSubmit: '提交失敗：',
        errFileSize: '檔案太大，最大 2GB',
        errFileType: '唔支援呢個格式，請用 MP4, M4A, WAV 或 MKV',
        errUploadHttp: 'Upload 失敗：HTTP ',
        errUploadNet: 'Upload 失敗，請檢查網絡連線',
        errUpload: 'Upload 失敗：',
        historyStatus_uploaded: '已上傳',
        historyStatus_processing: '處理中',
        historyStatus_completed: '完成',
        historyStatus_refined: '已調整',
        historyStatus_failed: '失敗',
        authEmail: 'Email',
        authPassword: '密碼',
        authCode: '驗證碼',
        btnSignIn: '登入',
        btnSignUp: '註冊',
        btnConfirm: '確認',
        noAccount: '冇帳號？',
        goSignUp: '註冊',
        hasAccount: '已有帳號？',
        goSignIn: '登入',
        passwordHint: '最少 8 個字，需要大小寫同數字',
        confirmInfo: '驗證碼已發送到你嘅 email。',
        contactAdmin: '如需帳號，請聯絡管理員。',
        forgotPassword: '忘記密碼？',
        newPasswordInfo: '首次登入，請設定新密碼。',
        newPassword: '新密碼',
        confirmPassword: '確認密碼',
        btnSetPassword: '設定密碼',
        forgotInfo: '輸入你嘅 email，我哋會發送重設密碼嘅驗證碼。',
        btnSendCode: '發送驗證碼',
        backToLogin: '返回登入',
        resetInfo: '驗證碼已發送到你嘅 email。',
        btnResetPassword: '重設密碼',
        errPasswordMismatch: '兩次輸入嘅密碼唔一樣',
        resetSuccess: '密碼已重設，請用新密碼登入。',
        changePassword: '更改密碼',
        currentPassword: '現有密碼',
        btnChangePassword: '更改密碼',
        passwordChanged: '密碼已更改成功。',
        downloadDocx: '下載 DOCX',
        refineTitle: '調整會議紀錄',
        refinePlaceholder: '例如：加多啲 action items、翻譯做英文、重點講 budget...',
        refineSubmit: '發送',
        refineLoading: 'AI 正在調整...',
        refineSuccess: '會議紀錄已更新。',
        backToHistory: '← 返回',
        pageDetail: '會議紀錄詳情',
    },
    en: {
        logoSub: 'Precis',
        loginSub: 'Meeting recordings → Structured meeting minutes',
        btnLogin: 'Sign In / Sign Up',
        navNew: 'New Recording',
        navHistory: 'History',
        navSettings: 'Settings',
        btnLogout: 'Sign Out',
        pageNew: 'New Recording',
        pageHistory: 'History',
        pageSettings: 'Settings',
        step1: 'Requirements',
        step2: 'Upload',
        step3: 'Done',
        requirementsTitle: 'Set Requirements',
        emailLabel: 'Email Address <span class="required">*</span>',
        emailHint: "We'll email you when it's done",
        outputLangLabel: 'Output Language',
        summaryLenLabel: 'Summary Length',
        lenShort: 'Short (~300 words)',
        lenMedium: 'Standard (~800 words)',
        lenDetailed: 'Detailed (~1500 words)',
        formatLabel: 'Output Format',
        fmtMinutes: 'Meeting Minutes',
        fmtActions: 'Action Items',
        fmtBoth: 'Both',
        customLabel: 'Additional Instructions',
        optional: '(optional)',
        btnNext: 'Next: Upload Recording',
        uploadTitle: 'Upload Meeting Recording',
        dropLabel: 'Drop your recording file here',
        or: 'or',
        browse: 'Browse Files',
        uploadHint: 'Supports MP4, M4A, WAV, MKV (max 2GB)',
        btnBack: 'Back to Requirements',
        doneTitle: 'Submitted!',
        doneText: 'Your meeting recording is being processed.',
        doneEmail: "We'll notify you at: ",
        doneEstimate: 'Estimated processing time: 15–30 minutes',
        btnNewJob: 'Submit Another Recording',
        historyEmpty: 'No Records Yet',
        historyEmptyText: 'Submit your first meeting recording.',
        settingsTitle: 'Settings',
        settingsText: 'Sign in to manage your account settings.',
        settingsAccount: 'Account',
        customPlaceholder: 'e.g. Focus on technical decisions, skip small talk, highlight timeline...',
        errEmail: 'Please enter your email address',
        errNoApi: 'API URL not configured. Deploy config.json first.',
        errSubmit: 'Submission failed: ',
        errFileSize: 'File too large, max 2GB',
        errFileType: 'Unsupported format. Use MP4, M4A, WAV or MKV',
        errUploadHttp: 'Upload failed: HTTP ',
        errUploadNet: 'Upload failed. Check your network connection.',
        errUpload: 'Upload failed: ',
        historyStatus_uploaded: 'Uploaded',
        historyStatus_processing: 'Processing',
        historyStatus_completed: 'Completed',
        historyStatus_refined: 'Refined',
        historyStatus_failed: 'Failed',
        authEmail: 'Email',
        authPassword: 'Password',
        authCode: 'Verification Code',
        btnSignIn: 'Sign In',
        btnSignUp: 'Sign Up',
        btnConfirm: 'Confirm',
        noAccount: "Don't have an account?",
        goSignUp: 'Sign Up',
        hasAccount: 'Already have an account?',
        goSignIn: 'Sign In',
        passwordHint: 'Min 8 characters, uppercase, lowercase and numbers',
        confirmInfo: 'A verification code has been sent to your email.',
        contactAdmin: 'Contact your administrator for an account.',
        forgotPassword: 'Forgot password?',
        newPasswordInfo: 'First login — please set a new password.',
        newPassword: 'New Password',
        confirmPassword: 'Confirm Password',
        btnSetPassword: 'Set Password',
        forgotInfo: "Enter your email and we'll send you a verification code.",
        btnSendCode: 'Send Code',
        backToLogin: 'Back to Sign In',
        resetInfo: 'A verification code has been sent to your email.',
        btnResetPassword: 'Reset Password',
        errPasswordMismatch: 'Passwords do not match',
        resetSuccess: 'Password reset. Please sign in with your new password.',
        changePassword: 'Change Password',
        currentPassword: 'Current Password',
        btnChangePassword: 'Change Password',
        passwordChanged: 'Password changed successfully.',
        downloadDocx: 'Download DOCX',
        refineTitle: 'Refine Meeting Minutes',
        refinePlaceholder: 'e.g. Add more action items, translate to English, focus on budget...',
        refineSubmit: 'Send',
        refineLoading: 'AI is refining...',
        refineSuccess: 'Meeting minutes updated.',
        backToHistory: '← Back',
        pageDetail: 'Meeting Minutes Detail',
    },
};

let currentLang = 'zh';

function t(key) {
    return I18N[currentLang][key] || I18N.zh[key] || key;
}

function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const text = t(key);
        if (el.hasAttribute('data-i18n-html')) {
            el.innerHTML = text;
        } else {
            el.textContent = text;
        }
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
    document.documentElement.lang = currentLang === 'zh' ? 'zh-Hant' : 'en';
}

function setLang(lang) {
    currentLang = lang;
    localStorage.setItem('precis-lang', lang);
    applyI18n();
    document.querySelectorAll('.lang-switcher__option').forEach(btn => {
        const isActive = btn.dataset.lang === lang;
        btn.classList.toggle('lang-switcher__option--active', isActive);
        btn.setAttribute('aria-checked', isActive);
    });
    document.querySelectorAll('.lang-switcher__slider').forEach(slider => {
        slider.classList.toggle('lang-switcher__slider--right', lang === 'en');
    });
}

// --- Theme ---
function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('precis-theme', theme);
}

function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    setTheme(current === 'dark' ? 'light' : 'dark');
}

function initTheme() {
    const saved = localStorage.getItem('precis-theme');
    if (saved) {
        setTheme(saved);
    } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        setTheme('dark');
    }
}

// --- DOM ---
const stepsEl = {};
let fileInput, uploadArea, progressContainer, progressFill, progressText, fileNameEl, fileSizeEl, errorBanner, errorMessage;

function initDom() {
    stepsEl.requirements = document.getElementById('step-requirements');
    stepsEl.upload = document.getElementById('step-upload');
    stepsEl.done = document.getElementById('step-done');
    fileInput = document.getElementById('file-input');
    uploadArea = document.getElementById('upload-area');
    progressContainer = document.getElementById('upload-progress');
    progressFill = document.getElementById('progress-fill');
    progressText = document.getElementById('progress-text');
    fileNameEl = document.getElementById('file-name');
    fileSizeEl = document.getElementById('file-size');
    errorBanner = document.getElementById('error-banner');
    errorMessage = document.getElementById('error-message');
}

let currentJobId = null;
let currentUploadUrl = null;
let currentStep = 1;

// --- Wizard Step Navigation ---
function updateStepIndicator(step) {
    const items = document.querySelectorAll('.wizard-steps__item');
    const connectors = document.querySelectorAll('.wizard-steps__connector');
    items.forEach((item, i) => {
        const num = i + 1;
        item.classList.remove('wizard-steps__item--current', 'wizard-steps__item--completed');
        const circle = item.querySelector('.wizard-steps__circle');
        if (num < step) {
            item.classList.add('wizard-steps__item--completed');
            circle.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
        } else if (num === step) {
            item.classList.add('wizard-steps__item--current');
            circle.textContent = num;
        } else {
            circle.textContent = num;
        }
    });
    connectors.forEach((conn, i) => {
        conn.classList.toggle('wizard-steps__connector--completed', i < step - 1);
    });
}

function showStep(name) {
    const stepMap = { requirements: 1, upload: 2, done: 3 };
    currentStep = stepMap[name];
    Object.values(stepsEl).forEach(el => el.classList.remove('wizard-panel--active'));
    stepsEl[name].classList.add('wizard-panel--active');
    updateStepIndicator(currentStep);
}

// --- Sidebar Navigation ---
function showPage(pageName) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('page--active'));
    const page = document.getElementById(`page-${pageName}`);
    if (page) page.classList.add('page--active');
    document.querySelectorAll('.sidebar__item[data-page]').forEach(item => {
        item.classList.toggle('sidebar__item--active', item.dataset.page === pageName);
    });
    const titleKey = `page${pageName.charAt(0).toUpperCase() + pageName.slice(1)}`;
    const titleEl = document.getElementById('page-title');
    titleEl.textContent = t(titleKey);
    titleEl.setAttribute('data-i18n', titleKey);

    if (pageName === 'history') loadHistory();
}

// --- Error ---
function showError(msg) {
    errorMessage.textContent = msg;
    errorBanner.hidden = false;
    setTimeout(() => { errorBanner.hidden = true; }, 8000);
}

// --- Step 1: Requirements ---
let pendingRequirements = null;

function initFormHandler() {
    const formEl = document.getElementById('requirements-form');
    formEl.addEventListener('submit', (e) => {
        e.preventDefault();
        const email = document.getElementById('email').value.trim();
        if (!email) return showError(t('errEmail'));
        if (!CONFIG.API_URL) return showError(t('errNoApi'));

        pendingRequirements = {
            email,
            output_language: document.getElementById('output-language').value,
            summary_length: document.getElementById('summary-length').value,
            output_format: document.querySelector('input[name="output-format"]:checked').value,
            custom_instructions: document.getElementById('custom-instructions').value.trim(),
        };
        showStep('upload');
    });
}

// Map file extension to MIME type for upload Content-Type header
function getContentType(file) {
    if (file.type) return file.type;
    const ext = file.name.split('.').pop().toLowerCase();
    const mimeMap = {
        mp4: 'video/mp4',
        m4a: 'audio/mp4',
        wav: 'audio/wav',
        mkv: 'video/x-matroska',
    };
    return mimeMap[ext] || 'application/octet-stream';
}

async function submitJobAndUpload(file) {
    const contentType = getContentType(file);
    const payload = { ...pendingRequirements, file_name: file.name, content_type: contentType };
    try {
        const res = await apiFetch(`${CONFIG.API_URL}/jobs`, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || `API error: ${res.status}`);
        }
        const data = await res.json();
        currentJobId = data.job_id;
        currentUploadUrl = data.upload_url;
        uploadFile(file, contentType);
    } catch (err) {
        showError(t('errSubmit') + err.message);
        resetUpload();
    }
}

// --- Step 2: Upload ---
function initUploadHandlers() {
    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });
    uploadArea.addEventListener('dragleave', () => uploadArea.classList.remove('dragover'));
    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        const file = e.dataTransfer.files[0];
        if (file) handleFile(file);
    });
    document.querySelector('.upload-zone__browse').addEventListener('click', (e) => {
        e.stopPropagation();
        fileInput.click();
    });
    uploadArea.addEventListener('click', (e) => {
        if (e.target.closest('.upload-zone__browse')) return;
        fileInput.click();
    });
    fileInput.addEventListener('change', () => {
        const file = fileInput.files[0];
        if (file) handleFile(file);
    });
}

function handleFile(file) {
    const maxSize = 2 * 1024 * 1024 * 1024;
    if (file.size > maxSize) return showError(t('errFileSize'));
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['mp4', 'm4a', 'wav', 'mkv'].includes(ext)) return showError(t('errFileType'));

    fileNameEl.textContent = file.name;
    fileSizeEl.textContent = formatSize(file.size);
    progressContainer.hidden = false;
    uploadArea.style.display = 'none';
    submitJobAndUpload(file);
}

async function uploadFile(file, contentType) {
    try {
        const xhr = new XMLHttpRequest();
        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const pct = Math.round((e.loaded / e.total) * 100);
                progressFill.style.width = `${pct}%`;
                progressText.textContent = `${pct}% (${formatSize(e.loaded)} / ${formatSize(e.total)})`;
            }
        });
        xhr.addEventListener('load', () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                progressFill.classList.remove('progress-bar__fill--active');
                progressFill.classList.add('progress-bar__fill--complete');
                document.getElementById('done-email').textContent = document.getElementById('email').value;
                document.getElementById('done-filename').textContent = fileNameEl.textContent;
                setTimeout(() => showStep('done'), 600);
            } else {
                showError(t('errUploadHttp') + xhr.status);
                resetUpload();
            }
        });
        xhr.addEventListener('error', () => {
            showError(t('errUploadNet'));
            resetUpload();
        });
        xhr.open('PUT', currentUploadUrl);
        xhr.setRequestHeader('Content-Type', contentType);
        xhr.send(file);
    } catch (err) {
        showError(t('errUpload') + err.message);
        resetUpload();
    }
}

function resetUpload() {
    progressContainer.hidden = true;
    uploadArea.style.display = '';
    progressFill.style.width = '0%';
    progressFill.classList.add('progress-bar__fill--active');
    progressFill.classList.remove('progress-bar__fill--complete');
    progressText.textContent = '0%';
    fileInput.value = '';
}

// --- History ---
async function loadHistory() {
    if (!CONFIG.API_URL || !currentUser) return;
    const listEl = document.getElementById('history-list');
    const emptyEl = document.getElementById('history-empty');

    try {
        const res = await apiFetch(
            `${CONFIG.API_URL}/jobs`,
            { headers: { 'Authorization': currentUser?.token || '' } }
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const jobs = data.jobs || [];

        if (jobs.length === 0) {
            listEl.innerHTML = '';
            emptyEl.hidden = false;
            return;
        }

        emptyEl.hidden = true;
        listEl.innerHTML = '';
        jobs.forEach(job => {
            const statusKey = `historyStatus_${job.status}`;
            const statusText = t(statusKey);
            let statusClass;
            if (job.status === 'completed' || job.status === 'refined') {
                statusClass = 'success';
            } else if (job.status === 'failed') {
                statusClass = 'error';
            } else if (job.status === 'processing') {
                statusClass = 'processing';
            } else {
                statusClass = 'default';
            }
            const date = job.created_at ? new Date(job.created_at).toISOString().slice(0, 10) : '';
            const fmt = job.requirements?.output_format || '';
            const lang = job.requirements?.output_language || '';

            const title = job.file_name || `${job.job_id.substring(0, 8)}...`;

            const item = document.createElement('div');
            item.className = 'history-item';

            const statusDot = document.createElement('div');
            statusDot.className = `history-item__status history-item__status--${statusClass}`;

            const body = document.createElement('div');
            body.className = 'history-item__body';

            const titleEl = document.createElement('div');
            titleEl.className = 'history-item__title';
            titleEl.textContent = title;

            const metaEl = document.createElement('div');
            metaEl.className = 'history-item__meta';
            metaEl.textContent = `${date} \u00b7 ${lang} \u00b7 ${fmt}`;

            body.appendChild(titleEl);
            body.appendChild(metaEl);

            const badge = document.createElement('span');
            badge.className = `history-item__badge history-item__badge--${statusClass}`;
            badge.textContent = statusText;

            if (job.status === 'completed' || job.status === 'refined') {
                item.style.cursor = 'pointer';
                item.addEventListener('click', () => showJobDetail(job.job_id, job.file_name));
            }

            item.appendChild(statusDot);
            item.appendChild(body);
            item.appendChild(badge);
            listEl.appendChild(item);
        });
    } catch {
        listEl.innerHTML = '';
        emptyEl.hidden = false;
    }
}

// --- Job Detail ---
let currentDetailJobId = null;

async function showJobDetail(jobId, fileName) {
    currentDetailJobId = jobId;
    document.getElementById('detail-title').textContent = fileName || jobId;
    document.getElementById('detail-minutes').innerHTML = '<p>Loading...</p>';
    document.getElementById('refine-input').value = '';
    document.getElementById('refine-error').hidden = true;

    showPage('detail');

    // Fetch the meeting minutes markdown from API
    try {
        const res = await apiFetch(`${CONFIG.API_URL}/jobs/${jobId}`, {
            headers: { 'Authorization': currentUser?.token || '' }
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        // Simple markdown to HTML rendering (basic)
        const html = simpleMarkdownToHtml(data.minutes || 'No content available');
        document.getElementById('detail-minutes').innerHTML = html;
    } catch (err) {
        document.getElementById('detail-minutes').innerHTML = '<p>Failed to load minutes.</p>';
    }
}

function escapeHtml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function simpleMarkdownToHtml(md) {
    // Escape HTML entities first to prevent XSS from LLM-generated content,
    // then apply markdown-to-HTML transformations on the safe string.
    return escapeHtml(md)
        .replace(/^### (.+)$/gm, '<h4>$1</h4>')
        .replace(/^## (.+)$/gm, '<h3>$1</h3>')
        .replace(/^# (.+)$/gm, '<h2>$1</h2>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        .replace(/^\* (.+)$/gm, '<li>$1</li>')
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/^\d+\.\s+(.+)$/gm, '<oli>$1</oli>')
        .replace(/(<oli>.*<\/oli>\n?)+/g, '<ol>$&</ol>')
        .replace(/(<li>.*<\/li>\n?)+/g, '<ul>$&</ul>')
        .replace(/<oli>/g, '<li>')
        .replace(/<\/oli>/g, '</li>')
        .replace(/^---$/gm, '<hr>')
        .replace(/\n\n/g, '</p><p>')
        .replace(/\n/g, '<br>');
}

// --- Utility ---
function formatSize(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return bytes + ' B';
}

// --- Event Listeners ---
function initEventListeners() {
    // Error close
    document.getElementById('error-close').addEventListener('click', () => {
        errorBanner.hidden = true;
    });

    // Back / New buttons
    document.getElementById('btn-back').addEventListener('click', () => {
        resetUpload();
        showStep('requirements');
    });
    document.getElementById('btn-new').addEventListener('click', () => {
        document.getElementById('requirements-form').reset();
        if (currentUser) document.getElementById('email').value = currentUser.email;
        resetUpload();
        currentJobId = null;
        currentUploadUrl = null;
        showStep('requirements');
    });

    // Sidebar nav
    document.querySelectorAll('.sidebar__item[data-page]').forEach(item => {
        item.addEventListener('click', () => showPage(item.dataset.page));
    });
    document.getElementById('sidebar-toggle').addEventListener('click', () => {
        document.querySelector('.layout').classList.toggle('sidebar-collapsed');
    });

    // Theme toggles (login + app)
    document.querySelectorAll('.theme-toggle').forEach(btn => {
        btn.addEventListener('click', toggleTheme);
    });

    // Language switchers (login + app)
    document.querySelectorAll('.lang-switcher__option').forEach(btn => {
        btn.addEventListener('click', () => setLang(btn.dataset.lang));
    });

    // Login form
    document.getElementById('auth-login').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('login-error');
        errEl.hidden = true;
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;
        try {
            const result = await signIn(email, password);
            if (result.challenge === 'NEW_PASSWORD_REQUIRED') {
                _showAuthForm('auth-new-password');
                return;
            }
            currentUser = result;
            showApp();
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        }
    });

    // New password form (first login)
    document.getElementById('auth-new-password').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('new-password-error');
        errEl.hidden = true;
        const pw = document.getElementById('new-password').value;
        const pw2 = document.getElementById('new-password-confirm').value;
        if (pw !== pw2) {
            errEl.textContent = t('errPasswordMismatch');
            errEl.hidden = false;
            return;
        }
        try {
            currentUser = await completeNewPassword(pw);
            showApp();
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        }
    });

    // Forgot password link
    document.getElementById('forgot-password-link').addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById('forgot-email').value = document.getElementById('login-email').value;
        _showAuthForm('auth-forgot');
    });

    // Back to login link
    document.getElementById('back-to-login').addEventListener('click', (e) => {
        e.preventDefault();
        _showAuthForm('auth-login');
    });

    // Forgot password form — send code
    let forgotEmail = '';
    document.getElementById('auth-forgot').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('forgot-error');
        errEl.hidden = true;
        forgotEmail = document.getElementById('forgot-email').value.trim();
        try {
            await forgotPassword(forgotEmail);
            _showAuthForm('auth-reset');
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        }
    });

    // Reset password form — enter code + new password
    document.getElementById('auth-reset').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('reset-error');
        errEl.hidden = true;
        const code = document.getElementById('reset-code').value.trim();
        const pw = document.getElementById('reset-password').value;
        try {
            await confirmForgotPassword(forgotEmail, code, pw);
            _showAuthForm('auth-login');
            const loginErr = document.getElementById('login-error');
            loginErr.textContent = t('resetSuccess');
            loginErr.hidden = false;
            loginErr.style.color = 'var(--color-success, #4caf50)';
            setTimeout(() => { loginErr.style.color = ''; }, 5000);
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        }
    });

    // Change password form
    document.getElementById('change-password-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = document.getElementById('change-password-error');
        const successEl = document.getElementById('change-password-success');
        errEl.hidden = true;
        successEl.hidden = true;

        const oldPw = document.getElementById('current-password').value;
        const newPw = document.getElementById('settings-new-password').value;
        const confirmPw = document.getElementById('settings-confirm-password').value;

        if (newPw !== confirmPw) {
            errEl.textContent = t('errPasswordMismatch');
            errEl.hidden = false;
            return;
        }
        try {
            await changePassword(oldPw, newPw);
            successEl.textContent = t('passwordChanged');
            successEl.hidden = false;
            document.getElementById('change-password-form').reset();
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        }
    });

    // Detail page — back button
    document.getElementById('detail-back').addEventListener('click', () => showPage('history'));

    // Detail page — refine submit
    document.getElementById('refine-submit').addEventListener('click', async () => {
        const instruction = document.getElementById('refine-input').value.trim();
        if (!instruction) return;

        const errEl = document.getElementById('refine-error');
        const loadingEl = document.getElementById('refine-loading');
        errEl.hidden = true;
        loadingEl.hidden = false;
        document.getElementById('refine-submit').disabled = true;

        try {
            const res = await apiFetch(`${CONFIG.API_URL}/jobs/${currentDetailJobId}/refine`, {
                method: 'POST',
                headers: authHeaders(),
                body: JSON.stringify({ instruction }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.error || `HTTP ${res.status}`);
            }
            const data = await res.json();
            document.getElementById('detail-minutes').innerHTML = simpleMarkdownToHtml(data.minutes);
            document.getElementById('refine-input').value = '';
        } catch (err) {
            errEl.textContent = err.message;
            errEl.hidden = false;
        } finally {
            loadingEl.hidden = true;
            document.getElementById('refine-submit').disabled = false;
        }
    });

    // Detail page — download DOCX: hidden because there is no presigned-URL
    // download endpoint yet. Users already receive the DOCX via email (SES).
    document.getElementById('detail-download').style.display = 'none';

    // Logout buttons
    document.getElementById('btn-logout').addEventListener('click', logout);
    document.getElementById('settings-logout').addEventListener('click', logout);
}

// --- Init ---
async function init() {
    initTheme();
    const savedLang = localStorage.getItem('precis-lang');
    if (savedLang) setLang(savedLang);

    await loadConfig();
    initDom();
    initEventListeners();
    initFormHandler();
    initUploadHandlers();

    // Check auth
    currentUser = loadAuthFromSession();
    if (currentUser) {
        showApp();
    } else {
        showLogin();
    }
}

init();
