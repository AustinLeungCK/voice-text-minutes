// Configuration — update after deployment
const CONFIG = {
    API_URL: '', // e.g. https://xxxxxx.execute-api.ap-east-1.amazonaws.com/prod
    API_KEY: '', // API Gateway API key
};

// DOM elements
const stepsEl = {
    requirements: document.getElementById('step-requirements'),
    upload: document.getElementById('step-upload'),
    done: document.getElementById('step-done'),
};

const form = document.getElementById('requirements-form');
const fileInput = document.getElementById('file-input');
const uploadArea = document.getElementById('upload-area');
const progressContainer = document.getElementById('upload-progress');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const fileNameEl = document.getElementById('file-name');
const fileSizeEl = document.getElementById('file-size');
const errorBanner = document.getElementById('error-banner');
const errorMessage = document.getElementById('error-message');

let currentJobId = null;
let currentUploadUrl = null;

// --- Step navigation ---
function showStep(name) {
    Object.values(stepsEl).forEach(el => el.classList.remove('active'));
    stepsEl[name].classList.add('active');
}

// --- Error handling ---
function showError(msg) {
    errorMessage.textContent = msg;
    errorBanner.hidden = false;
    setTimeout(() => { errorBanner.hidden = true; }, 8000);
}

document.getElementById('error-close').addEventListener('click', () => {
    errorBanner.hidden = true;
});

// --- Step 1: Requirements form submit ---
form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const email = document.getElementById('email').value.trim();
    if (!email) return showError('請填寫 email 地址');

    if (!CONFIG.API_URL) return showError('API URL 未設定，請更新 app.js CONFIG');

    const requirements = {
        email,
        output_language: document.getElementById('output-language').value,
        summary_length: document.getElementById('summary-length').value,
        output_format: document.querySelector('input[name="output-format"]:checked').value,
        custom_instructions: document.getElementById('custom-instructions').value.trim(),
    };

    try {
        const res = await fetch(`${CONFIG.API_URL}/jobs`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'x-api-key': CONFIG.API_KEY,
            },
            body: JSON.stringify(requirements),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || `API error: ${res.status}`);
        }

        const data = await res.json();
        currentJobId = data.job_id;
        currentUploadUrl = data.upload_url;

        showStep('upload');
    } catch (err) {
        showError(`提交失敗：${err.message}`);
    }
});

// --- Step 2: File upload ---

// Drag and drop
uploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadArea.classList.add('dragover');
});

uploadArea.addEventListener('dragleave', () => {
    uploadArea.classList.remove('dragover');
});

uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
});

uploadArea.addEventListener('click', () => {
    fileInput.click();
});

fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) handleFile(file);
});

function handleFile(file) {
    const maxSize = 2 * 1024 * 1024 * 1024; // 2GB
    if (file.size > maxSize) {
        return showError('檔案太大，最大 2GB');
    }

    const ext = file.name.split('.').pop().toLowerCase();
    if (!['mp4', 'm4a', 'wav', 'mkv'].includes(ext)) {
        return showError('唔支援呢個格式，請用 MP4, M4A, WAV 或 MKV');
    }

    fileNameEl.textContent = file.name;
    fileSizeEl.textContent = formatSize(file.size);
    progressContainer.hidden = false;
    uploadArea.style.display = 'none';

    uploadFile(file);
}

async function uploadFile(file) {
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
                // Upload success — show done
                document.getElementById('done-email').textContent = document.getElementById('email').value;
                document.getElementById('done-job-id').textContent = currentJobId;
                showStep('done');
            } else {
                showError(`Upload 失敗：HTTP ${xhr.status}`);
                resetUpload();
            }
        });

        xhr.addEventListener('error', () => {
            showError('Upload 失敗，請檢查網絡連線');
            resetUpload();
        });

        xhr.open('PUT', currentUploadUrl);
        xhr.setRequestHeader('Content-Type', 'video/mp4');
        xhr.send(file);
    } catch (err) {
        showError(`Upload 失敗：${err.message}`);
        resetUpload();
    }
}

function resetUpload() {
    progressContainer.hidden = true;
    uploadArea.style.display = '';
    progressFill.style.width = '0%';
    progressText.textContent = '0%';
    fileInput.value = '';
}

// --- Step 2: Back button ---
document.getElementById('btn-back').addEventListener('click', () => {
    resetUpload();
    showStep('requirements');
});

// --- Step 3: New submission ---
document.getElementById('btn-new').addEventListener('click', () => {
    form.reset();
    resetUpload();
    currentJobId = null;
    currentUploadUrl = null;
    showStep('requirements');
});

// --- Utility ---
function formatSize(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return bytes + ' B';
}
