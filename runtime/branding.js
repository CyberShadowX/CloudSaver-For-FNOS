(() => {
  'use strict';
  const oldName = 'Cloud Saver';
  const newName = 'CloudSaver';
  const repositoryLink = /^(https:\/\/github\.com\/(?:jiangrui1994|CyberShadowX)\/)cloudsaver(?=\/|[?#]|$)/i;
  let images = [];
  let headings = [];
  let links = [];

  const refresh = () => {
    for (const image of images) {
      const alt = image.getAttribute('alt');
      if (alt === oldName || alt === `${oldName} Logo`) {
        image.setAttribute('alt', alt === oldName ? newName : `${newName} Logo`);
      }
    }
    for (const heading of headings) {
      if (heading.textContent === oldName) heading.textContent = newName;
    }
    for (const link of links) {
      const href = link.getAttribute('href');
      if (href === null) continue;
      const normalized = href.replace(repositoryLink, '$1CloudSaver');
      if (normalized !== href) link.setAttribute('href', normalized);
    }
    if (document.title === oldName) document.title = newName;
  };

  const observer = new MutationObserver(refresh);
  const discover = () => {
    observer.disconnect();
    images = Array.from(document.querySelectorAll('img.logo__image, img.login__logo'));
    const selected = new Set(document.querySelectorAll('h1.login__title'));
    for (const image of images) {
      if (!image.classList.contains('logo__image')) continue;
      const container = image.closest('div');
      const heading = container && container.querySelector(':scope > h1');
      if (heading) selected.add(heading);
    }
    headings = Array.from(selected);
    links = Array.from(document.querySelectorAll('a.github-link'));
    refresh();
    for (const image of images) {
      observer.observe(image, {attributes: true, attributeFilter: ['alt']});
    }
    for (const heading of headings) {
      observer.observe(heading, {childList: true, characterData: true, subtree: true});
    }
    for (const link of links) {
      observer.observe(link, {attributes: true, attributeFilter: ['href']});
    }
    const title = document.querySelector('title');
    if (title) observer.observe(title, {childList: true, characterData: true, subtree: true});
  };

  discover();
  // Selector-only discovery handles Vue route remounts. The observer watches
  // the fixed brand elements above, never the application/resource subtree.
  window.setInterval(discover, 1000);
})();
