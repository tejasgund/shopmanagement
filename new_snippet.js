// Helper: delegate clicks for any element with data-edit-agreement
document.body.addEventListener('click', function (e) {
  const btn = e.target.closest('[data-edit-agreement]');
  if (!btn) return;
  const shopId = Number(btn.dataset.editAgreement);
  const shopNumber = btn.dataset.shopNumber || '';
  const startIso = btn.dataset.start || '';
  const endIso = btn.dataset.end || '';
  const rentDay = btn.dataset.rentDay || '';
  const userId = btn.dataset.userId ? Number(btn.dataset.userId) : null;
  const userName = btn.dataset.name || '';
  // If openEditAgreementModal is defined on the page, call it
  if (typeof openEditAgreementModal === 'function') {
    openEditAgreementModal(userId, userName, shopId, shopNumber, startIso, endIso, rentDay);
  }
});