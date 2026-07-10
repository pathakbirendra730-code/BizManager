/*
 * static/js/otp-entry.js
 * ======================
 * Shared controller for every 6-digit OTP entry box in the app — signup
 * email/mobile verification, PIN reset, App Admin login 2FA, SaaS
 * "Login with OTP", and change-email/change-mobile confirmation all use
 * this one function instead of each re-implementing their own version.
 *
 * ROOT CAUSE this fixes (BizManager-v6 Update_013, Issue 1):
 * -------------------------------------------------------------
 * Calling form.submit() from JavaScript does NOT fire the form's native
 * 'submit' event — that's a long-standing, easy-to-miss browser behavior
 * (event listeners on 'submit' only run for user-initiated submissions:
 * a real click on a submit button, or Enter in a field).
 *
 * Every OTP screen auto-submits 400ms after the 6th digit is typed, as a
 * convenience. Every one of them ALSO left the "Verify" button enabled
 * the instant 6 digits were entered — so if someone typed the last digit
 * and then immediately tapped the (now-enabled) button, the manual
 * click's real submit and the timer's auto-submit could both go out as
 * two separate POST requests. Whichever request the server processed
 * SECOND would find the OTP already consumed by the first and correctly
 * report "No OTP request found" — but the browser sometimes rendered
 * that second, failed response instead of following the first request's
 * (successful) redirect, so the person saw an error despite having
 * entered the right code and actually being logged in.
 *
 * The previous code's guard flag was only ever set INSIDE the 'submit'
 * event listener — which, per the above, never runs for the timer's
 * programmatic form.submit() call, so the guard did nothing for exactly
 * the case it needed to prevent.
 *
 * THE FIX: one `submitted` flag, flipped to true synchronously at the
 * very start of whichever path fires first — the auto-submit timer or a
 * real user submit — and checked by both. JavaScript is single-threaded,
 * so whichever runs first wins, and the other is guaranteed to see the
 * flag already set and back off. No second request is ever sent.
 */
function initOtpEntry(opts) {
  opts = opts || {};
  const form   = document.getElementById(opts.formId || 'otpForm');
  const hidden = document.getElementById(opts.hiddenId || 'otpHidden');
  const btn    = document.getElementById(opts.btnId || 'verifyBtn');
  const digits = document.querySelectorAll(opts.digitSelector || '.otp-digit');
  if (!form || !hidden || !btn || !digits.length) return null;

  const submittingText = opts.submittingText || 'Verifying…';
  const autoSubmitDelay = opts.autoSubmitDelay || 300;

  let submitted = false;
  let autoSubmitTimer;

  function syncHidden() {
    const otp = [...digits].map(d => d.value).join('');
    hidden.value = otp;
    btn.disabled = otp.length !== digits.length;
    if (otp.length === digits.length) btn.classList.add('ready');
    return otp;
  }

  function lockUI() {
    digits.forEach(d => d.disabled = true);
    btn.disabled = true;
    btn.textContent = submittingText;
  }

  function guardedSubmit() {
    if (submitted) return;      // <- the actual fix: set BEFORE submit(),
    submitted = true;           //    not inside a listener that won't fire.
    lockUI();
    form.submit();
  }

  function maybeAutoSubmit(otp) {
    if (otp.length !== digits.length) return;
    clearTimeout(autoSubmitTimer);
    autoSubmitTimer = setTimeout(guardedSubmit, autoSubmitDelay);
  }

  digits.forEach((el, i) => {
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !el.value && i > 0) digits[i - 1].focus();
    });
    el.addEventListener('input', (e) => {
      const val = e.target.value.replace(/\D/g, '');
      e.target.value = val ? val[0] : '';
      if (val && i < digits.length - 1) digits[i + 1].focus();
      maybeAutoSubmit(syncHidden());
    });
    el.addEventListener('paste', (e) => {
      e.preventDefault();
      const pasted = (e.clipboardData || window.clipboardData)
                      .getData('text').replace(/\D/g, '').slice(0, digits.length);
      pasted.split('').forEach((ch, idx) => { if (digits[idx]) digits[idx].value = ch; });
      if (pasted.length === digits.length) digits[digits.length - 1].focus();
      maybeAutoSubmit(syncHidden());
    });
  });

  // A real user submit (button click or Enter) goes through the native
  // 'submit' event, which DOES fire — so this catches the case where the
  // click wins the race against a pending auto-submit timer.
  form.addEventListener('submit', function(e) {
    if (submitted) { e.preventDefault(); return; }
    submitted = true;
    lockUI();
  });

  return { syncHidden, guardedSubmit };
}
