// Whether this page is the testnet sandbox (pay-sandbox.7331.org, PLAN.md section 16). The API marks the sandbox's
// index.html with `<meta name="pay-deployment" content="sandbox">` (backend app/payments/pages/router.py, from
// PAY_SANDBOX), so the banner shows on the first paint with no request and no inline script.
export const deploymentIsSandbox =
  document.querySelector('meta[name="pay-deployment"]')?.getAttribute("content") === "sandbox"
