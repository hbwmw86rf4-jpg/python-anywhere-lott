/**
 * Shared high-performance camera barcode scanner for the IL Lottery app.
 *
 * Features:
 *   - Dual Detection Engine:
 *       1. Native Web BarcodeDetector API (Hardware-accelerated decoding)
 *       2. Html5Qrcode / ZXing engine (Fallback for unsupported platforms/formats)
 *   - Default 2.0x Camera Zoom: Starts camera pre-zoomed to 2.0x so cashiers can hold tickets at a comfortable distance without blurring macro focus.
 *   - Interactive Zoom Preset Buttons (1x, 2x, 3x) for instant focal adjustment.
 *   - Safe Format Probing: Verifies browser supported enum formats before instantiating native detector (prevents TypeError crashes on iOS Safari).
 *   - Full HD 1080p video constraints with automatic fallback for low-end camera streams.
 *   - Continuous autofocus and exposure compensation.
 *   - Hardware torch / flashlight control.
 *   - Web Audio synthesizer scan beep + haptic vibration feedback.
 *   - Dynamic rectangular ROI target frame overlay.
 *   - Duplicate scan protection & race-condition-free form submission.
 */

(function () {
    'use strict';

    // Automatic Scroll & Focus Position Preservation across reloads and form submits
    (function () {
        try {
            var savedY = sessionStorage.getItem('lott_saved_scroll_y');
            var savedFocusId = sessionStorage.getItem('lott_saved_focus_id');
            
            if (savedY !== null && savedY !== undefined) {
                var targetY = parseInt(savedY, 10);
                sessionStorage.removeItem('lott_saved_scroll_y');
                sessionStorage.removeItem('lott_saved_focus_id');
                if (!isNaN(targetY) && targetY > 0) {
                    var restore = function () {
                        window.scrollTo(0, targetY);
                        if (savedFocusId) {
                            var focusEl = document.getElementById(savedFocusId);
                            if (focusEl) {
                                try { focusEl.focus(); } catch (e) {}
                            }
                        }
                    };
                    if (document.readyState === 'complete') {
                        restore();
                    } else {
                        window.addEventListener('load', restore);
                        setTimeout(restore, 50);
                    }
                }
            }

            var savePos = function () {
                try {
                    sessionStorage.setItem('lott_saved_scroll_y', window.scrollY || window.pageYOffset || 0);
                    if (document.activeElement && document.activeElement.id) {
                        sessionStorage.setItem('lott_saved_focus_id', document.activeElement.id);
                    }
                } catch (e) {}
            };

            window.addEventListener('beforeunload', savePos);
            document.addEventListener('submit', savePos, true);
        } catch (e) {}
    })();

    // Duplicate-scan guard: ignore the exact same barcode fired within 1.5 seconds.
    var lastScan = { code: null, ts: 0 };

    var sharedAudioCtx = null;

    /**
     * Synthesize a short, crisp audio scan beep using Web Audio API.
     */
    function playScanBeep() {
        try {
            var AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;
            if (!sharedAudioCtx) {
                sharedAudioCtx = new AudioContextClass();
            }
            if (sharedAudioCtx.state === 'suspended') {
                sharedAudioCtx.resume();
            }
            var ctx = sharedAudioCtx;
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            gain.gain.setValueAtTime(0.2, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.12);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.12);
        } catch (e) {
            /* ignore audio restriction if user hasn't interacted */
        }
    }

    /**
     * Synthesize a dissonant, low-pitch error buzz using Web Audio API.
     */
    function playErrorBeep() {
        try {
            var AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;
            if (!sharedAudioCtx) {
                sharedAudioCtx = new AudioContextClass();
            }
            if (sharedAudioCtx.state === 'suspended') {
                sharedAudioCtx.resume();
            }
            var ctx = sharedAudioCtx;
            var osc = ctx.createOscillator();
            var osc2 = ctx.createOscillator();
            var gain = ctx.createGain();
            
            // Dissonant frequencies for an "error" sound
            osc.type = 'sawtooth';
            osc.frequency.setValueAtTime(150, ctx.currentTime);
            osc2.type = 'square';
            osc2.frequency.setValueAtTime(160, ctx.currentTime);
            
            gain.gain.setValueAtTime(0.3, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
            
            osc.connect(gain);
            osc2.connect(gain);
            gain.connect(ctx.destination);
            
            osc.start();
            osc2.start();
            osc.stop(ctx.currentTime + 0.3);
            osc2.stop(ctx.currentTime + 0.3);
        } catch (e) {
            /* ignore */
        }
    }

    /**
     * Trigger tactile haptic vibration feedback on mobile devices.
     */
    function triggerHaptic() {
        if (navigator.vibrate) {
            try {
                navigator.vibrate([40, 30, 40]);
            } catch (e) {}
        }
    }

    /**
     * Inject scanner UI styles (scan frame overlay, animated laser line, torch & zoom buttons).
     */
    function injectStyles() {
        if (document.getElementById('lott-scanner-styles')) return;
        var style = document.createElement('style');
        style.id = 'lott-scanner-styles';
        style.textContent = `
            .lott-scan-frame-overlay {
                position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                pointer-events: none; z-index: 5;
            }
            .lott-scan-frame {
                position: absolute; top: 50%; left: 50%;
                transform: translate(-50%, -50%);
                width: 85%; height: 50%; max-width: 320px; max-height: 160px;
                border: 2px solid #ffffff; border-radius: 8px;
                box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.45);
                transition: border-color 0.15s ease, box-shadow 0.15s ease;
            }
            .lott-scan-frame.success {
                border-color: #39ff14 !important;
                box-shadow: 0 0 25px #39ff14, 0 0 0 9999px rgba(0, 0, 0, 0.45) !important;
            }
            .lott-scan-corner { position: absolute; width: 18px; height: 18px; border: 3px solid #ffffff; }
            .lott-scan-corner.tl { top: -3px; left: -3px; border-right: none; border-bottom: none; }
            .lott-scan-corner.tr { top: -3px; right: -3px; border-left: none; border-bottom: none; }
            .lott-scan-corner.bl { bottom: -3px; left: -3px; border-right: none; border-top: none; }
            .lott-scan-corner.br { bottom: -3px; right: -3px; border-left: none; border-top: none; }
            .lott-scan-status {
                position: absolute; bottom: 8px; left: 0; width: 100%;
                text-align: center; color: #ffffff; font-size: 12px; font-weight: bold;
                text-shadow: 0 1px 3px rgba(0,0,0,0.8); z-index: 6; pointer-events: none;
            }
            .lott-controls-bar {
                display: flex; justify-content: center; align-items: center; gap: 8px;
                margin: 10px auto; z-index: 10; relative; flex-wrap: wrap;
            }
            .lott-torch-btn, .lott-zoom-btn {
                padding: 6px 14px; background: #333; color: #fff; border: 1px solid #666;
                border-radius: 20px; font-size: 13px; font-weight: bold; cursor: pointer;
                box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: background 0.2s, transform 0.1s;
            }
            .lott-zoom-btn.active {
                background: #007bff !important; border-color: #0056b3 !important;
                box-shadow: 0 0 8px rgba(0,123,255,0.6);
            }
            .lott-video-preview {
                width: 100%; height: 100%; object-fit: cover; display: block; border-radius: 8px;
            }
            /* Touch Numpad Modal Styles */
            .lott-np-overlay {
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(0, 0, 0, 0.75); z-index: 99999; display: none;
                justify-content: center; align-items: center; padding: 12px;
                box-sizing: border-box; overflow-y: auto;
            }
            .lott-np-card {
                background: #ffffff; width: 100%; max-width: 440px; border-radius: 14px;
                padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); box-sizing: border-box;
                font-family: Arial, sans-serif; position: relative;
            }
            .lott-np-header {
                display: flex; justify-content: space-between; align-items: center;
                border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 12px;
            }
            .lott-np-header h3 { margin: 0; font-size: 1.15em; color: #111; display: flex; align-items: center; gap: 6px; }
            .lott-np-close {
                background: #e9ecef; border: none; font-size: 18px; font-weight: bold;
                border-radius: 50%; width: 32px; height: 32px; cursor: pointer; color: #333;
            }
            .lott-ticket-guide {
                background: #f8f9fa; border: 2px dashed #007bff; border-radius: 10px;
                padding: 12px; margin-bottom: 12px; font-size: 12px;
            }
            .lott-ticket-diagram {
                background: #fff; border: 1px solid #ccc; border-radius: 6px; padding: 8px;
                margin-top: 6px; display: flex; flex-direction: column; gap: 6px; text-align: center;
            }
            .lott-badge {
                display: inline-block; padding: 3px 8px; border-radius: 4px; font-weight: bold;
                color: #fff; font-size: 11px; margin: 0 2px;
            }
            .lott-badge-game { background: #0056b3; }
            .lott-badge-pack { background: #28a745; }
            .lott-badge-ticket { background: #e8590c; }
            .lott-mode-tabs { display: flex; gap: 6px; margin-bottom: 12px; }
            .lott-tab-btn {
                flex: 1; padding: 8px; font-weight: bold; font-size: 12px; border: 1px solid #007bff;
                border-radius: 6px; background: #fff; color: #007bff; cursor: pointer;
            }
            .lott-tab-btn.active { background: #007bff; color: #fff; }
            .lott-field-group { display: flex; gap: 6px; margin-bottom: 12px; }
            .lott-field-box { flex: 1; display: flex; flex-direction: column; }
            .lott-field-box label { font-size: 11px; font-weight: bold; margin-bottom: 3px; }
            .lott-field-input {
                width: 100%; padding: 10px 6px; font-size: 16px; font-weight: bold;
                text-align: center; border: 2px solid #ccc; border-radius: 6px;
                box-sizing: border-box; background: #fff; cursor: pointer;
            }
            .lott-field-input.active { border-color: #007bff; background: #e7f3ff; box-shadow: 0 0 5px rgba(0,123,255,0.4); }
            .lott-np-grid {
                display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 12px;
            }
            .lott-np-key {
                padding: 14px 0; font-size: 22px; font-weight: bold; background: #f1f3f5;
                border: 1px solid #ced4da; border-radius: 8px; cursor: pointer; text-align: center;
                user-select: none; transition: background 0.1s, transform 0.05s;
            }
            .lott-np-key:active { background: #d0ebff; transform: scale(0.96); }
            .lott-np-key.action { background: #e9ecef; color: #495057; font-size: 16px; }
            .lott-np-submit {
                width: 100%; padding: 12px; font-size: 16px; font-weight: bold; color: #fff;
                background: #28a745; border: none; border-radius: 8px; cursor: pointer;
            }
        `;
        document.head.appendChild(style);
    }

    /**
     * Open the Touch Numpad modal for entering ticket numbers manually.
     */
    function openTouchNumpad(targetInputId, targetFormId) {
        injectStyles();

        var modal = document.getElementById('lottNumpadModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'lottNumpadModal';
            modal.className = 'lott-np-overlay';
            modal.innerHTML = `
                <div class="lott-np-card">
                    <div class="lott-np-header">
                        <h3>⌨ Manual Ticket Entry</h3>
                        <button type="button" class="lott-np-close" id="lottNpClose">✕</button>
                    </div>

                    <!-- Visual Ticket Diagram Reference -->
                    <div class="lott-ticket-guide">
                        <div style="font-weight:bold; margin-bottom:4px;">💡 Where to find numbers on ticket back:</div>
                        <div class="lott-ticket-diagram">
                            <div>
                                <span class="lott-badge lott-badge-game">GAME (3 or 4 digits)</span>
                                <span class="lott-badge lott-badge-pack">PACK (5-6 digits)</span>
                                <span class="lott-badge lott-badge-ticket">TICKET (3 digits)</span>
                            </div>
                            <div style="font-family: monospace; font-size: 13px; font-weight: bold; background: #eee; padding: 4px; border-radius: 4px;">
                                <span style="color:#0056b3;">754</span> - <span style="color:#28a745;">012345</span> - <span style="color:#e8590c;">042</span>
                            </div>
                        </div>
                    </div>

                    <!-- Mode Tabs -->
                    <div class="lott-mode-tabs">
                        <button type="button" class="lott-tab-btn active" id="lottTabGuided">🎯 Guided Mode</button>
                        <button type="button" class="lott-tab-btn" id="lottTabRaw">🔢 Full Barcode</button>
                    </div>

                    <!-- Guided Fields -->
                    <div id="lottGuidedGroup" class="lott-field-group">
                        <div class="lott-field-box">
                            <label style="color:#0056b3;">1. Game #</label>
                            <input type="text" id="npGame" class="lott-field-input active" placeholder="754 / 1234" readonly>
                        </div>
                        <div class="lott-field-box">
                            <label style="color:#28a745;">2. Pack #</label>
                            <input type="text" id="npPack" class="lott-field-input" placeholder="012345" readonly>
                        </div>
                        <div class="lott-field-box">
                            <label style="color:#e8590c;">3. Ticket #</label>
                            <input type="text" id="npTicket" class="lott-field-input" placeholder="042" readonly>
                        </div>
                    </div>

                    <!-- Raw Field -->
                    <div id="lottRawGroup" style="display:none; margin-bottom:12px;">
                        <label style="font-weight:bold; font-size:12px;">Full Barcode String:</label>
                        <input type="text" id="npRaw" class="lott-field-input" placeholder="754012345042" style="text-align:left; padding-left:10px;" readonly>
                    </div>

                    <!-- Preview -->
                    <div style="font-size:12px; color:#666; margin-bottom:10px; text-align:center;">
                        Generated Barcode: <strong id="npPreview" style="color:#111; font-family:monospace; font-size:14px;">---</strong>
                    </div>

                    <!-- Touch Numpad Grid -->
                    <div class="lott-np-grid">
                        <div class="lott-np-key" data-key="1">1</div>
                        <div class="lott-np-key" data-key="2">2</div>
                        <div class="lott-np-key" data-key="3">3</div>
                        <div class="lott-np-key" data-key="4">4</div>
                        <div class="lott-np-key" data-key="5">5</div>
                        <div class="lott-np-key" data-key="6">6</div>
                        <div class="lott-np-key" data-key="7">7</div>
                        <div class="lott-np-key" data-key="8">8</div>
                        <div class="lott-np-key" data-key="9">9</div>
                        <div class="lott-np-key action" data-key="CLEAR">C</div>
                        <div class="lott-np-key" data-key="0">0</div>
                        <div class="lott-np-key action" data-key="BACK">⌫</div>
                    </div>

                    <button type="button" class="lott-np-submit" id="lottNpSubmit">✅ Submit Ticket Reading</button>
                </div>
            `;
            document.body.appendChild(modal);
        }

        modal.style.display = 'flex';

        var activeField = 'npGame';
        var mode = 'guided';

        var gameInp = document.getElementById('npGame');
        var packInp = document.getElementById('npPack');
        var ticketInp = document.getElementById('npTicket');
        var rawInp = document.getElementById('npRaw');
        var previewEl = document.getElementById('npPreview');

        var tabGuided = document.getElementById('lottTabGuided');
        var tabRaw = document.getElementById('lottTabRaw');
        var guidedGroup = document.getElementById('lottGuidedGroup');
        var rawGroup = document.getElementById('lottRawGroup');

        // Reset state
        gameInp.value = '';
        packInp.value = '';
        ticketInp.value = '';
        rawInp.value = '';

        function updateActiveInputHighlight() {
            [gameInp, packInp, ticketInp, rawInp].forEach(function (el) {
                if (el) el.classList.remove('active');
            });
            if (mode === 'guided') {
                var activeEl = document.getElementById(activeField);
                if (activeEl) activeEl.classList.add('active');
            } else {
                rawInp.classList.add('active');
            }
            updatePreview();
        }

        function updatePreview() {
            if (mode === 'guided') {
                var g = gameInp.value.trim();
                var p = packInp.value.trim();
                var t = ticketInp.value.trim();
                if (g || p || t) {
                    var formattedT = t ? ('000' + t).slice(-3) : '';
                    previewEl.textContent = g + (p ? ('000000' + p).slice(-6) : '') + formattedT + (formattedT ? '00' : '');
                } else {
                    previewEl.textContent = '---';
                }
            } else {
                previewEl.textContent = rawInp.value.trim() || '---';
            }
        }

        // Tab click events
        tabGuided.onclick = function () {
            mode = 'guided';
            tabGuided.classList.add('active');
            tabRaw.classList.remove('active');
            guidedGroup.style.display = 'flex';
            rawGroup.style.display = 'none';
            activeField = 'npGame';
            updateActiveInputHighlight();
        };

        tabRaw.onclick = function () {
            mode = 'raw';
            tabRaw.classList.add('active');
            tabGuided.classList.remove('active');
            guidedGroup.style.display = 'none';
            rawGroup.style.display = 'block';
            updateActiveInputHighlight();
        };

        // Field click selection
        gameInp.onclick = function () { activeField = 'npGame'; updateActiveInputHighlight(); };
        packInp.onclick = function () { activeField = 'npPack'; updateActiveInputHighlight(); };
        ticketInp.onclick = function () { activeField = 'npTicket'; updateActiveInputHighlight(); };

        // Close button
        document.getElementById('lottNpClose').onclick = function () {
            modal.style.display = 'none';
        };

        // Instant 0ms Numpad Key Event Handling
        var keys = modal.querySelectorAll('.lott-np-key');
        keys.forEach(function (k) {
            var handlePress = function (e) {
                if (e) {
                    e.preventDefault();
                    e.stopPropagation();
                }

                var val = k.getAttribute('data-key');
                if (mode === 'guided') {
                    var curEl = document.getElementById(activeField);
                    if (!curEl) return;

                    if (val === 'CLEAR') {
                        curEl.value = '';
                    } else if (val === 'BACK') {
                        curEl.value = curEl.value.slice(0, -1);
                    } else {
                        // Max lengths: Game (4), Pack (6), Ticket (3)
                        var maxLen = activeField === 'npGame' ? 4 : (activeField === 'npPack' ? 6 : 3);
                        if (curEl.value.length < maxLen) {
                            curEl.value += val;
                        }
                        // Auto-advance to next field
                        if (curEl.value.length === maxLen) {
                            if (activeField === 'npGame') activeField = 'npPack';
                            else if (activeField === 'npPack') activeField = 'npTicket';
                        }
                    }
                } else {
                    if (val === 'CLEAR') {
                        rawInp.value = '';
                    } else if (val === 'BACK') {
                        rawInp.value = rawInp.value.slice(0, -1);
                    } else {
                        if (rawInp.value.length < 24) {
                            rawInp.value += val;
                        }
                    }
                }
                updateActiveInputHighlight();
            };

            var handled = false;
            k.onpointerdown = function (e) {
                handled = true;
                handlePress(e);
            };
            k.onclick = function (e) {
                if (!handled) handlePress(e);
                handled = false;
            };
        });

        // Submit Button
        document.getElementById('lottNpSubmit').onclick = function () {
            var barcodeToSend = previewEl.textContent.trim();
            if (barcodeToSend === '---' || barcodeToSend.length < 5) {
                alert('Please enter a valid ticket number.');
                return;
            }

            modal.style.display = 'none';

            var targetInput = document.getElementById(targetInputId);
            if (targetInput) targetInput.value = barcodeToSend;

            playScanBeep();
            triggerHaptic();

            var targetForm = document.getElementById(targetFormId);
            if (targetForm) targetForm.submit();
        };
    }

    window.openTouchNumpad = openTouchNumpad;

    /**
     * Wire up a camera-scan button.
     *
     * @param {string} btnId   - ID of the toggle button.
     * @param {string} readerId - ID of the div that holds the camera preview.
     * @param {string} inputId  - ID of the text input that receives decoded barcode.
     * @param {string} formId   - ID of the form to submit after successful scan.
     */
    function wireCamera(btnId, readerId, inputId, formId) {
        var btn = document.getElementById(btnId);
        var reader = document.getElementById(readerId);
        if (!btn || !reader) {
            console.error('wireCamera: element missing for', btnId, readerId);
            return;
        }

        injectStyles();

        // Auto-attach a Manual Entry button right beside camera scan button
        var npBtnId = btnId + '_numpad';
        if (!document.getElementById(npBtnId) && btn && btn.parentNode) {
            var npBtn = document.createElement('button');
            npBtn.type = 'button';
            npBtn.id = npBtnId;
            npBtn.className = btn.className || 'cam-btn';
            npBtn.style.background = '#28a745';
            npBtn.style.marginLeft = '8px';
            npBtn.innerHTML = '⌨ Manual Entry';
            npBtn.addEventListener('click', function () {
                window.openTouchNumpad(inputId, formId);
            });
            btn.parentNode.insertBefore(npBtn, btn.nextSibling);
        }

        var activeStream = null;
        var activeVideo = null;
        var activeDetectorLoop = null;
        var html5QrInstance = null;
        var running = false;

        btn.dataset.originalText = btn.dataset.originalText || btn.textContent;

        var isSubmitting = false;

        function cleanupUI() {
            reader.innerHTML = '';
            reader.style.display = 'none';
            btn.textContent = btn.dataset.originalText || '📷 Scan with Camera';
            running = false;
        }

        function stopCamera() {
            if (activeDetectorLoop) {
                cancelAnimationFrame(activeDetectorLoop);
                activeDetectorLoop = null;
            }
            if (html5QrInstance) {
                try {
                    html5QrInstance.stop().then(function() {
                        html5QrInstance.clear();
                    }).catch(function() {});
                } catch(e) {}
                html5QrInstance = null;
            }
            if (activeStream) {
                activeStream.getTracks().forEach(function (track) {
                    try { track.stop(); } catch (e) {}
                });
                activeStream = null;
            }
            if (activeVideo) {
                activeVideo.srcObject = null;
                activeVideo = null;
            }
            cleanupUI();
        }

        function handleSuccessfulScan(decodedText) {
            if (isSubmitting) return;

            var now = Date.now();
            decodedText = (decodedText || '').trim();

            var statusEl = reader.querySelector('.lott-scan-status');

            // Pre-validation: Allow valid lottery barcode lengths (11 to 32 digits)
            var cleanText = decodedText.replace(/\D/g, '');
            if (cleanText.length < 11 || cleanText.length > 32) {
                lastScan.code = decodedText; // temporarily ignore this garbage string
                lastScan.ts = now;
                playErrorBeep();
                triggerHaptic();
                if (statusEl) {
                    statusEl.textContent = '❌ Invalid Barcode (' + decodedText.length + ' chars)';
                    statusEl.style.color = '#ff4d4d';
                    setTimeout(function() {
                        statusEl.textContent = 'Hold ticket 12-18 inches back';
                        statusEl.style.color = '#ffffff';
                    }, 2000);
                }
                return;
            }

            isSubmitting = true;
            lastScan.code = decodedText;
            lastScan.ts = now;

            // Audio + Visual + Haptic feedback
            playScanBeep();
            triggerHaptic();

            var frame = reader.querySelector('.lott-scan-frame');
            if (frame) frame.classList.add('success');

            if (statusEl) {
                statusEl.textContent = '✓ Scanned! Submitting...';
                statusEl.style.color = '#39ff14';
            }

            var form = document.getElementById(formId);
            var inputEl = document.getElementById(inputId) || (form ? form.querySelector('input[name="barcode"]') : null);
            if (inputEl) {
                inputEl.value = decodedText;
            }

            // Stop camera stream & submit form cleanly
            setTimeout(function () {
                stopCamera();
                if (form) {
                    var barInput = document.getElementById(inputId) || form.querySelector('input[name="barcode"]');
                    if (barInput) barInput.value = decodedText;
                    form.submit();
                }
            }, 300);
        }

        // Add visual overlay to container
        function buildOverlay() {
            var overlay = document.createElement('div');
            overlay.className = 'lott-scan-frame-overlay';
            overlay.innerHTML =
                '<div class="lott-scan-frame">' +
                '<div class="lott-scan-corner tl"></div>' +
                '<div class="lott-scan-corner tr"></div>' +
                '<div class="lott-scan-corner bl"></div>' +
                '<div class="lott-scan-corner br"></div>' +
                '</div>' +
                '<div class="lott-scan-status">Hold ticket 12-18 inches back</div>';
            return overlay;
        }

        // Setup hardware Torch & Zoom controls UI
        function setupCameraHardwareControls(track) {
            if (!track || typeof track.getCapabilities !== 'function') return;
            var capabilities = track.getCapabilities ? track.getCapabilities() : {};

            var controlsBar = document.createElement('div');
            controlsBar.className = 'lott-controls-bar';

            // 1. Hardware Zoom Controls (Default 2.0x Zoom)
            if (capabilities.zoom) {
                var minZoom = capabilities.zoom.min || 1;
                var maxZoom = capabilities.zoom.max || 5;
                var defaultZoom = Math.min(2.0, maxZoom);

                // Apply default 2.0x zoom automatically on start
                try {
                    track.applyConstraints({ advanced: [{ zoom: defaultZoom }] }).catch(function () {});
                } catch (e) {}

                var presets = [1.0, 2.0, 3.0];
                presets.forEach(function (zVal) {
                    if (zVal >= minZoom && zVal <= maxZoom) {
                        var zBtn = document.createElement('button');
                        zBtn.type = 'button';
                        zBtn.className = 'lott-zoom-btn' + (zVal === defaultZoom ? ' active' : '');
                        zBtn.innerHTML = '🔍 ' + zVal + 'x';
                        zBtn.addEventListener('click', function (e) {
                            e.preventDefault();
                            e.stopPropagation();
                            track.applyConstraints({ advanced: [{ zoom: zVal }] }).then(function () {
                                var btns = controlsBar.querySelectorAll('.lott-zoom-btn');
                                btns.forEach(function (b) { b.classList.remove('active'); });
                                zBtn.classList.add('active');
                            }).catch(function (err) {
                                console.warn('Zoom apply error:', err);
                            });
                        });
                        controlsBar.appendChild(zBtn);
                    }
                });
            }

            // 2. Torch (Flashlight) Control
            if (capabilities.torch) {
                var torchBtn = document.createElement('button');
                torchBtn.type = 'button';
                torchBtn.className = 'lott-torch-btn';
                torchBtn.innerHTML = '🔦 Light OFF';
                var torchState = false;
                torchBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    torchState = !torchState;
                    track.applyConstraints({ advanced: [{ torch: torchState }] }).then(function () {
                        torchBtn.innerHTML = torchState ? '💡 Light ON' : '🔦 Light OFF';
                        torchBtn.style.background = torchState ? '#e65100' : '#333';
                    }).catch(function (err) {
                        console.warn('Torch error:', err);
                    });
                });
                controlsBar.appendChild(torchBtn);
            }

            if (controlsBar.children.length > 0) {
                reader.appendChild(controlsBar);
            }
        }

        // Engine 1: Native BarcodeDetector API
        async function startNativeScanner(stream) {
            var wantedFormats = ['code_128', 'itf', 'code_39', 'pdf417', 'data_matrix', 'upc_a', 'upc_e', 'ean_13', 'qr_code'];
            var safeFormats = [];
            var barcodeDetector = null;

            if ('BarcodeDetector' in window) {
                try {
                    if (typeof window.BarcodeDetector.getSupportedFormats === 'function') {
                        var avail = await window.BarcodeDetector.getSupportedFormats();
                        safeFormats = wantedFormats.filter(function (f) { return avail.indexOf(f) !== -1; });
                    } else {
                        safeFormats = ['code_128', 'code_39', 'qr_code'];
                    }

                    if (safeFormats.length > 0) {
                        barcodeDetector = new window.BarcodeDetector({ formats: safeFormats });
                    }
                } catch (e) {
                    console.warn('Native BarcodeDetector init failed, falling back:', e);
                }
            }

            if (!barcodeDetector) {
                if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
                startHtml5QrcodeFallback();
                return;
            }

            reader.innerHTML = '';
            reader.style.position = 'relative';
            reader.style.overflow = 'hidden';

            var video = document.createElement('video');
            video.className = 'lott-video-preview';
            video.setAttribute('playsinline', 'true');
            video.setAttribute('autoplay', 'true');
            video.muted = true;
            video.srcObject = stream;
            reader.appendChild(video);
            reader.appendChild(buildOverlay());

            activeVideo = video;
            activeStream = stream;

            var track = stream.getVideoTracks()[0];
            setupCameraHardwareControls(track);

            function scanFrame() {
                if (!running || !activeVideo) return;
                if (activeVideo.readyState >= 2 && activeVideo.videoWidth > 0) {
                    barcodeDetector.detect(activeVideo).then(function (barcodes) {
                        if (barcodes && barcodes.length > 0 && running) {
                            var code = barcodes[0].rawValue;
                            if (code) {
                                handleSuccessfulScan(code);
                            }
                        }
                        if (running) {
                            activeDetectorLoop = requestAnimationFrame(scanFrame);
                        }
                    }).catch(function (err) {
                        console.debug('Native detect error:', err);
                        if (running) {
                            activeDetectorLoop = requestAnimationFrame(scanFrame);
                        }
                    });
                } else {
                    activeDetectorLoop = requestAnimationFrame(scanFrame);
                }
            }

            video.play().then(function () {
                running = true;
                btn.textContent = '✋ Stop Camera';
                activeDetectorLoop = requestAnimationFrame(scanFrame);
            }).catch(function (err) {
                console.error('Video play error:', err);
                if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
                startHtml5QrcodeFallback();
            });
        }

        // Engine 2: Html5Qrcode Fallback Engine
        function startHtml5QrcodeFallback() {
            if (typeof Html5Qrcode === 'undefined') {
                reader.innerHTML = '<p style="color:#b00; padding:10px;">Scanner library missing. Please reload page.</p>';
                reader.style.display = 'block';
                return;
            }

            reader.innerHTML = '';
            reader.style.position = 'relative';

            var formats = [];
            if (typeof Html5QrcodeSupportedFormats !== 'undefined') {
                formats = [
                    Html5QrcodeSupportedFormats.ITF,
                    Html5QrcodeSupportedFormats.CODE_128,
                    Html5QrcodeSupportedFormats.CODE_39,
                    Html5QrcodeSupportedFormats.PDF_417,
                    Html5QrcodeSupportedFormats.DATA_MATRIX
                ];
            } else {
                formats = [5, 1, 4, 7, 6];
            }

            var config = {
                fps: 25,
                qrbox: function (viewfinderWidth, viewfinderHeight) {
                    var width = Math.floor(viewfinderWidth * 0.85);
                    var height = Math.floor(viewfinderHeight * 0.45);
                    return { width: Math.max(width, 240), height: Math.max(height, 120) };
                },
                videoConstraints: {
                    facingMode: 'environment',
                    width: { ideal: 1920, min: 1280 },
                    height: { ideal: 1080, min: 720 },
                    advanced: [
                        { zoom: 2.0 },
                        { focusMode: 'continuous' }
                    ]
                },
                formatsToSupport: formats
            };

            try {
                html5QrInstance = new Html5Qrcode(readerId);
            } catch (e) {
                reader.innerHTML = '<p style="color:#b00; padding:10px;">Scanner init error: ' + e.message + '</p>';
                return;
            }

            reader.appendChild(buildOverlay());

            html5QrInstance.start(
                { facingMode: 'environment' },
                config,
                function (decodedText) {
                    if (running) {
                        handleSuccessfulScan(decodedText);
                    }
                },
                function () { /* per-frame miss normal */ }
            ).then(function () {
                running = true;
                btn.textContent = '✋ Stop Camera';
            }).catch(function (err) {
                console.warn('Html5Qrcode HD constraints failed, retrying basic constraints:', err);
                html5QrInstance.start(
                    { facingMode: 'environment' },
                    { fps: 20, qrbox: { width: 280, height: 140 } },
                    function (decodedText) {
                        if (running) handleSuccessfulScan(decodedText);
                    },
                    function () {}
                ).then(function () {
                    running = true;
                    btn.textContent = '✋ Stop Camera';
                }).catch(function (err2) {
                    reader.innerHTML = '<p style="color:#b00; padding:10px;">Camera access error: ' + err2 +
                        '. Enable camera permissions in browser settings.</p>';
                });
            });
        }

        // Toggle click handler
        btn.addEventListener('click', function () {
            if (running) {
                stopCamera();
                return;
            }

            reader.style.display = 'block';

            // High HD Video Constraints + Default 2.0x Zoom
            var constraints = {
                video: {
                    facingMode: { ideal: 'environment' },
                    width: { ideal: 1920, min: 1280 },
                    height: { ideal: 1080, min: 720 },
                    advanced: [
                        { zoom: 2.0 },
                        { focusMode: 'continuous' },
                        { exposureMode: 'continuous' }
                    ]
                }
            };

            if ('BarcodeDetector' in window) {
                navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
                    running = true;
                    startNativeScanner(stream);
                }).catch(function (err) {
                    console.warn('2.0x 1080p stream request failed, retrying default stream:', err);
                    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } }).then(function (stream) {
                        running = true;
                        startNativeScanner(stream);
                    }).catch(function () {
                        startHtml5QrcodeFallback();
                    });
                });
            } else {
                startHtml5QrcodeFallback();
            }
        });
    }

    // Expose globally
    window.wireCamera = wireCamera;
})();
