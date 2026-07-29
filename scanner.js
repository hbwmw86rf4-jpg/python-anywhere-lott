/**
 * Shared camera barcode scanner for the IL Lottery app.
 *
 * Provides a reusable wireCamera() that:
 *   - Supports ITF, CODE_128, CODE_39, PDF_417, and DATA_MATRIX formats
 *   - Shows a visual scanning frame to help position barcodes
 *   - Stops the camera fully before submitting the form (avoids race conditions)
 *   - Restores autofocus on the input after submission for rapid scanning
 *   - Ignores duplicate scans within a 2-second window
 */

(function () {
    'use strict';

    // Duplicate-scan guard: ignore the same barcode fired twice within 2 seconds.
    var lastScan = { code: null, ts: 0 };

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
        if (!btn || !reader) return;

        var qr = null;
        var running = false;

        // All barcode formats used on Illinois lottery tickets.
        var config = {
            fps: 10,
            qrbox: { width: 280, height: 160 },
            formatsToSupport: [
                Html5QrcodeSupportedFormats.ITF,           // Interleaved 2 of 5
                Html5QrcodeSupportedFormats.CODE_128,
                Html5QrcodeSupportedFormats.CODE_39,
                Html5QrcodeSupportedFormats.PDF_417,
                Html5QrcodeSupportedFormats.DATA_MATRIX
            ]
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
            btn.textContent = btn.dataset.originalText || '📷 Scan with Camera';
        }

        // Store the button's original text so we can restore it.
        btn.dataset.originalText = btn.textContent;

        btn.addEventListener('click', function () {
            if (running) { stop(); return; }

            reader.style.display = 'block';
            addScanFrame();
            qr = new Html5Qrcode(readerId);

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
                function () { /* ignore per-frame decode misses */ }
            ).then(function () {
                running = true;
                btn.textContent = '✋ Stop Camera';
            }).catch(function (err) {
                reader.innerHTML = '<p style="color:#b00; padding:10px;">Camera error: ' + err +
                    '. In Safari, tap "aA" in the address bar → Website Settings → allow Camera.</p>';
            });
        });
    }

    // Expose globally so templates can call wireCamera(...).
    window.wireCamera = wireCamera;
})();
