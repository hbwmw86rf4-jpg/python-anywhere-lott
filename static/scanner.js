/**
 * Shared camera barcode scanner for the IL Lottery app.
 *
 * Provides a reusable wireCamera() that:
 *   - Supports ITF, CODE_128, CODE_39, PDF_417, and DATA_MATRIX formats
 *   - Shows a visual scanning frame to help position barcodes
 *   - Stops the camera fully before submitting the form (avoids race conditions)
 *   - Ignores duplicate scans within a 2-second window
 *   - Handles errors gracefully with user-friendly messages
 */

(function () {
    'use strict';

    // Duplicate-scan guard: ignore the same barcode fired twice within 2 seconds.
    var lastScan = { code: null, ts: 0 };

    // Hardcoded format constants as fallback when Html5QrcodeSupportedFormats
    // is not available. These are the numeric values used by html5-qrcode v2.x:
    //   ITF = 5, CODE_39 = 1, CODE_128 = 4, PDF_417 = 7, DATA_MATRIX = 6
    // Using these ensures the scanner only tries 1D/2D barcode formats used
    // on lottery tickets, NOT QR codes or Aztec codes (which would be slow).
    var FALLBACK_FORMATS = [5, 1, 4, 7, 6];

    /**
     * Wire up a camera-scan button.
     *
     * @param {string} btnId   - ID of the toggle button.
     * @param {string} readerId - ID of the div that holds the camera preview.
     * @param {string} inputId  - ID of the text input that receives the decoded text.
     * @param {string} formId   - ID of the form to submit after a successful scan.
     */
    function wireCamera(btnId, readerId, inputId, formId) {
        var btn = document.getElementById(btnId);
        var reader = document.getElementById(readerId);
        if (!btn || !reader) {
            console.error('wireCamera: button or reader not found for', btnId, readerId);
            return;
        }

        // Check that the html5-qrcode library is loaded.
        if (typeof Html5Qrcode === 'undefined') {
            reader.innerHTML = '<p style="color:#b00; padding:10px;">Scanner library not loaded. ' +
                'Please refresh the page. If the problem persists, check your internet connection.</p>';
            reader.style.display = 'block';
            return;
        }

        var qr = null;
        var running = false;

        // Build the formats list. Try Html5QrcodeSupportedFormats first,
        // fall back to hardcoded numeric constants.
        var supportedFormats = [];
        if (typeof Html5QrcodeSupportedFormats !== 'undefined') {
            var formatMap = {
                'ITF': Html5QrcodeSupportedFormats.ITF,
                'CODE_39': Html5QrcodeSupportedFormats.CODE_39,
                'CODE_128': Html5QrcodeSupportedFormats.CODE_128,
                'PDF_417': Html5QrcodeSupportedFormats.PDF_417,
                'DATA_MATRIX': Html5QrcodeSupportedFormats.DATA_MATRIX
            };
            for (var name in formatMap) {
                if (formatMap[name] !== undefined && formatMap[name] !== null) {
                    supportedFormats.push(formatMap[name]);
                }
            }
        }

        // Always use a non-empty formatsToSupport. If the enum wasn't available
        // or returned no formats, use the hardcoded fallback. This is critical:
        // an empty or undefined formatsToSupport makes the library try ALL
        // formats (QR, Aztec, etc.), which is extremely slow.
        if (supportedFormats.length === 0) {
            supportedFormats = FALLBACK_FORMATS;
        }

        var config = {
            fps: 20,
            qrbox: { width: 300, height: 200 },
            formatsToSupport: supportedFormats
        };

        // Inject a visual scanning frame into the reader div.
        function addScanFrame() {
            if (reader.querySelector('.scan-frame-overlay')) return;
            var overlay = document.createElement('div');
            overlay.className = 'scan-frame-overlay';
            overlay.innerHTML =
                '<div class="scan-frame">' +
                '<div class="scan-corner tl"></div>' +
                '<div class="scan-corner tr"></div>' +
                '<div class="scan-corner bl"></div>' +
                '<div class="scan-corner br"></div>' +
                '<div class="scan-line"></div>' +
                '</div>';
            reader.appendChild(overlay);
        }

        function removeScanFrame() {
            var el = reader.querySelector('.scan-frame-overlay');
            if (el) el.remove();
        }

        function stop() {
            if (qr && running) {
                qr.stop().then(function () {
                    qr.clear();
                }).catch(function () {});
            }
            running = false;
            removeScanFrame();
            reader.style.display = 'none';
            btn.textContent = btn.dataset.originalText || 'Scan with Camera';
        }

        // Store the button's original text so we can restore it.
        btn.dataset.originalText = btn.textContent;

        btn.addEventListener('click', function () {
            if (running) { stop(); return; }

            reader.style.display = 'block';
            addScanFrame();

            try {
                qr = new Html5Qrcode(readerId);
            } catch (e) {
                reader.innerHTML = '<p style="color:#b00; padding:10px;">Failed to initialize scanner: ' + e.message +
                    '. Please refresh the page and try again.</p>';
                return;
            }

            qr.start(
                { facingMode: 'environment' },
                config,
                function (decodedText) {
                    // Duplicate-scan guard.
                    var now = Date.now();
                    if (lastScan.code === decodedText && (now - lastScan.ts) < 2000) {
                        return; // ignore rapid duplicate
                    }
                    lastScan.code = decodedText;
                    lastScan.ts = now;

                    document.getElementById(inputId).value = decodedText;
                    stop();

                    // Small delay to let the camera fully release before form submit.
                    setTimeout(function () {
                        var form = document.getElementById(formId);
                        if (form) form.submit();
                    }, 150);
                },
                function (errorMessage) {
                    // Per-frame decode miss — this is normal, just ignore.
                    if (errorMessage && typeof errorMessage === 'string' && errorMessage.indexOf('error') !== -1) {
                        console.debug('Scan frame error:', errorMessage);
                    }
                }
            ).then(function () {
                running = true;
                btn.textContent = 'Stop Camera';
            }).catch(function (err) {
                reader.innerHTML = '<p style="color:#b00; padding:10px;">Camera error: ' + err +
                    '. In Safari, tap "aA" in the address bar → Website Settings → allow Camera.</p>';
            });
        });
    }

    // Expose globally so templates can call wireCamera(...).
    window.wireCamera = wireCamera;
})();
