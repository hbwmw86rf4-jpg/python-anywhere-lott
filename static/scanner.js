/**
 * Shared high-performance camera barcode scanner for the IL Lottery app.
 *
 * Features:
 *   - Dual Detection Engine:
 *       1. Native Web BarcodeDetector API (Hardware-accelerated decoding)
 *       2. Html5Qrcode / ZXing engine (Fallback for unsupported platforms/formats)
 *   - Safe Format Probing: Verifies browser supported enum formats before instantiating native detector (prevents TypeError crashes on iOS Safari)
 *   - Full HD 1080p video constraints with automatic fallback for low-end camera streams
 *   - Continuous autofocus and exposure compensation
 *   - Hardware torch / flashlight control (if device camera supports it)
 *   - Web Audio synthesizer scan beep + haptic vibration feedback
 *   - Dynamic rectangular ROI target frame overlay
 *   - Duplicate scan protection & race-condition-free form submission
 */

(function () {
    'use strict';

    // Duplicate-scan guard: ignore the exact same barcode fired within 1.5 seconds.
    var lastScan = { code: null, ts: 0 };

    /**
     * Synthesize a short, crisp audio scan beep using Web Audio API.
     */
    function playScanBeep() {
        try {
            var AudioContextClass = window.AudioContext || window.webkitAudioContext;
            if (!AudioContextClass) return;
            var ctx = new AudioContextClass();
            var osc = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(880, ctx.currentTime);
            gain.gain.setValueAtTime(0.3, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.12);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start();
            osc.stop(ctx.currentTime + 0.12);
            setTimeout(function () { ctx.close(); }, 200);
        } catch (e) {
            /* ignore audio restriction if user hasn't interacted */
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
     * Inject scanner UI styles (scan frame overlay, animated laser line, torch button).
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
                border: 2px solid #00ff00; border-radius: 8px;
                box-shadow: 0 0 0 9999px rgba(0, 0, 0, 0.45);
                transition: border-color 0.15s ease, box-shadow 0.15s ease;
            }
            .lott-scan-frame.success {
                border-color: #39ff14 !important;
                box-shadow: 0 0 25px #39ff14, 0 0 0 9999px rgba(0, 0, 0, 0.45) !important;
            }
            .lott-scan-corner { position: absolute; width: 18px; height: 18px; border: 3px solid #00ff00; }
            .lott-scan-corner.tl { top: -3px; left: -3px; border-right: none; border-bottom: none; }
            .lott-scan-corner.tr { top: -3px; right: -3px; border-left: none; border-bottom: none; }
            .lott-scan-corner.bl { bottom: -3px; left: -3px; border-right: none; border-top: none; }
            .lott-scan-corner.br { bottom: -3px; right: -3px; border-left: none; border-top: none; }
            .lott-scan-line {
                position: absolute; top: 0; left: 0; width: 100%; height: 2px;
                background: #00ff00; box-shadow: 0 0 8px #00ff00;
                animation: lott-scan-anim 2s infinite ease-in-out;
            }
            @keyframes lott-scan-anim {
                0% { top: 5%; opacity: 0.6; }
                50% { top: 90%; opacity: 1; }
                100% { top: 5%; opacity: 0.6; }
            }
            .lott-scan-status {
                position: absolute; bottom: 8px; left: 0; width: 100%;
                text-align: center; color: #ffffff; font-size: 12px; font-weight: bold;
                text-shadow: 0 1px 3px rgba(0,0,0,0.8); z-index: 6; pointer-events: none;
            }
            .lott-torch-btn {
                position: relative; z-index: 10; margin: 10px auto; display: block;
                padding: 8px 16px; background: #333; color: #fff; border: 1px solid #666;
                border-radius: 20px; font-size: 13px; font-weight: bold; cursor: pointer;
                box-shadow: 0 2px 5px rgba(0,0,0,0.2); transition: background 0.2s;
            }
            .lott-video-preview {
                width: 100%; height: 100%; object-fit: cover; display: block; border-radius: 8px;
            }
        `;
        document.head.appendChild(style);
    }

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

        var activeStream = null;
        var activeVideo = null;
        var activeDetectorLoop = null;
        var html5QrInstance = null;
        var running = false;

        btn.dataset.originalText = btn.dataset.originalText || btn.textContent;

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
            var now = Date.now();
            if (lastScan.code === decodedText && (now - lastScan.ts) < 1500) {
                return; // Suppress rapid duplicate scans
            }
            lastScan.code = decodedText;
            lastScan.ts = now;

            // Audio + Visual + Haptic feedback
            playScanBeep();
            triggerHaptic();

            var frame = reader.querySelector('.lott-scan-frame');
            if (frame) frame.classList.add('success');

            var inputEl = document.getElementById(inputId);
            if (inputEl) {
                inputEl.value = decodedText;
            }

            // Stop camera fully before submitting form to avoid camera lock / race conditions
            setTimeout(function () {
                stopCamera();
                setTimeout(function () {
                    var form = document.getElementById(formId);
                    if (form) form.submit();
                }, 120);
            }, 250);
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
                '<div class="lott-scan-line"></div>' +
                '</div>' +
                '<div class="lott-scan-status">Center barcode inside frame</div>';
            return overlay;
        }

        // Setup hardware torch toggle if supported by camera track
        function setupTorch(track) {
            if (!track || typeof track.getCapabilities !== 'function') return;
            var capabilities = track.getCapabilities();
            if (capabilities.torch) {
                var torchBtn = document.createElement('button');
                torchBtn.type = 'button';
                torchBtn.className = 'lott-torch-btn';
                torchBtn.innerHTML = '🔦 Flashlight OFF';
                var torchState = false;
                torchBtn.addEventListener('click', function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    torchState = !torchState;
                    track.applyConstraints({
                        advanced: [{ torch: torchState }]
                    }).then(function () {
                        torchBtn.innerHTML = torchState ? '💡 Flashlight ON' : '🔦 Flashlight OFF';
                        torchBtn.style.background = torchState ? '#e65100' : '#333';
                    }).catch(function (err) {
                        console.warn('Torch constraint error:', err);
                    });
                });
                reader.appendChild(torchBtn);
            }
        }

        // Engine 1: Native BarcodeDetector API (Safe instantiation with format check)
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

            // Fallback if native detector couldn't be initialized
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
            setupTorch(track);

            function scanFrame() {
                if (!running || !activeVideo) return;
                if (activeVideo.readyState >= 2 && activeVideo.videoWidth > 0) {
                    barcodeDetector.detect(activeVideo).then(function (barcodes) {
                        if (barcodes && barcodes.length > 0 && running) {
                            var code = barcodes[0].rawValue;
                            if (code) {
                                handleSuccessfulScan(code);
                                return;
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

        // Engine 2: Html5Qrcode Engine (High HD resolution & dynamic ROI box)
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
                    advanced: [{ focusMode: 'continuous' }]
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
                // Fallback to basic camera constraints if device rejected HD constraints
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

            // Check if Native BarcodeDetector API is present
            if ('BarcodeDetector' in window) {
                var constraints = {
                    video: {
                        facingMode: { ideal: 'environment' },
                        width: { ideal: 1920, min: 1280 },
                        height: { ideal: 1080, min: 720 },
                        advanced: [
                            { focusMode: 'continuous' },
                            { exposureMode: 'continuous' }
                        ]
                    }
                };

                navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
                    running = true;
                    startNativeScanner(stream);
                }).catch(function (err) {
                    console.warn('1080p stream request failed, retrying default constraints:', err);
                    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } }).then(function (stream) {
                        running = true;
                        startNativeScanner(stream);
                    }).catch(function () {
                        // Fallback to html5-qrcode
                        startHtml5QrcodeFallback();
                    });
                });
            } else {
                // Device/Browser doesn't have native BarcodeDetector API -> use high-res Html5Qrcode
                startHtml5QrcodeFallback();
            }
        });
    }

    // Expose globally
    window.wireCamera = wireCamera;
})();
