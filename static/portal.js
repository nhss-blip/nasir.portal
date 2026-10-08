// Core navigation, forms and deletion confirmation work without JavaScript.
document.querySelectorAll('[data-draft-warning]').forEach(form => {
  let dirty = false;
  form.addEventListener('input', () => { dirty = true; });
  form.addEventListener('change', () => { dirty = true; });
  form.addEventListener('submit', () => { dirty = false; });
  window.addEventListener('beforeunload', event => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
});

document.querySelectorAll('[data-upload-form]').forEach(form => {
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    form.querySelectorAll('button').forEach(button => { button.disabled = false; });
    form.querySelector('.upload-status').hidden = true;
  });
  form.addEventListener('submit', () => {
    const status = form.querySelector('.upload-status');
    status.hidden = false;
    status.textContent = 'Saving… Please keep this page open until the upload finishes.';
    // Defer disabling so browser validation and form data construction complete.
    setTimeout(() => form.querySelectorAll('button[type="submit"], button:not([type])').forEach(button => {
      button.disabled = true;
    }), 0);
  });
});

if (document.querySelector('[data-service-url]')) {
  fetch('/api/new', {cache: 'no-store'}).then(response => {
    if (!response.ok) throw new Error('Bad badge response');
    return response.json();
  }).then(fresh => {
    document.querySelectorAll('[data-service-url]').forEach(card => {
      if (!fresh[card.dataset.serviceUrl]) return;
      const badge = document.createElement('span');
      badge.className = 'badge gold new-badge';
      badge.textContent = 'New';
      card.querySelector('.service-copy').prepend(badge);
    });
  }).catch(() => { /* Services remain available when badges cannot load. */ });
}
