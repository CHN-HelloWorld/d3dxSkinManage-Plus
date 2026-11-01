(function () {
  'use strict';

  var root = document.documentElement;
  var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var dataElement = document.getElementById('reader-data');
  var data = JSON.parse(dataElement.textContent);
  dataElement.remove();
  var pages = data.pages, assets = data.assets, initialLanguage = data.initial;
  var activeLanguage = initialLanguage, switchToken = 0, busy = false, transition = null;
  var main = document.getElementById('main'), side = document.getElementById('side');
  var toc = document.getElementById('toc'), wrap = document.getElementById('wrap');
  var top = document.getElementById('top'), reading = document.getElementById('reading');
  var languageControl = document.getElementById('languages');
  var languageOptions = Array.from(languageControl.querySelectorAll('a[lang]'));
  var q = document.getElementById('q'), burger = document.getElementById('burger');
  var links = [], heads = [], map = {}, current = null, activeLink = null, scrollFrame = 0;
  history.scrollRestoration = 'manual';

  document.querySelectorAll('img[data-source][src]').forEach(function (img) {
    assets[img.dataset.source] = img.getAttribute('src');
  });
  pages[initialLanguage].mainNodes = null;
  pages[initialLanguage].tocNodes = null;

  function filterContents() {
    var keyword = q.value.trim().toLowerCase();
    links.forEach(function (a) {
      a.classList.toggle('hide', !!keyword && !a.textContent.toLowerCase().includes(keyword));
    });
  }

  function readingEdge() { return top.offsetHeight + reading.offsetHeight + 16; }

  function updateReading() {
    scrollFrame = 0;
    var edge = readingEdge() + 8, selected = null;
    var atEnd = window.scrollY > 0 && Math.ceil(window.scrollY + innerHeight) >= root.scrollHeight;
    for (var i = 0; i < heads.length; i++) {
      if (heads[i].getBoundingClientRect().top > (atEnd ? innerHeight : edge)) break;
      selected = heads[i];
    }
    current = selected;
    var id = current ? current.id : 'cover';
    var translated = current ? current.dataset.translationId : 'cover';
    languageOptions.forEach(function (a) {
      var target = a.lang === activeLanguage ? id : translated;
      var url = new URL(location.href);
      url.searchParams.set('lang', a.lang);
      url.hash = target || 'cover';
      a.href = url.href;
    });
    var nextLink = current && map[current.id];
    if (activeLink !== nextLink) {
      if (activeLink) activeLink.classList.remove('on');
      activeLink = nextLink;
      if (activeLink) {
        activeLink.classList.add('on');
        var rect = activeLink.getBoundingClientRect(), bounds = side.getBoundingClientRect();
        if (activeLink.offsetHeight && (rect.top < bounds.top + 55 || rect.bottom > bounds.bottom - 28)) {
          side.scrollTop += rect.top - bounds.top - side.clientHeight / 2;
        }
      }
    }
    document.getElementById('reading-location').textContent = current ? current.textContent : pages[activeLanguage].labels.START;
    var progress = Math.max(0, Math.min(1, window.scrollY / Math.max(1, root.scrollHeight - innerHeight)));
    document.getElementById('reading-fill').style.transform = 'scaleX(' + progress + ')';
    document.getElementById('reading-percent').textContent = Math.round(progress * 100) + '%';
    document.getElementById('reading-progress').setAttribute('aria-valuenow', String(Math.round(progress * 100)));
  }

  function queueReading() {
    if (!scrollFrame) scrollFrame = requestAnimationFrame(updateReading);
  }

  function bindReader() {
    links = Array.from(toc.querySelectorAll('a'));
    heads = Array.from(main.querySelectorAll('h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]'));
    map = {};
    links.forEach(function (a) { map[a.getAttribute('href').slice(1)] = a; a.classList.remove('on'); });
    activeLink = null;
    filterContents();
    bindPictures();
    document.getElementById('section-count').textContent = String(heads.length);
  }

  function updateLabels() {
    var labels = pages[activeLanguage].labels;
    root.lang = activeLanguage;
    document.title = 'd3dxSkinManage-Plus ' + labels.TITLE;
    document.getElementById('brand-title').textContent = labels.TITLE;
    document.getElementById('skip-link').textContent = labels.SKIP;
    document.getElementById('side-label').textContent = labels.CONTENTS;
    document.getElementById('reading-label').textContent = labels.READING;
    document.getElementById('reading-top').textContent = labels.TOP;
    document.getElementById('reading-progress').setAttribute('aria-label', labels.PROGRESS);
    document.getElementById('theme').textContent = labels.THEME;
    document.getElementById('lb-fit').textContent = labels.FIT;
    document.getElementById('lb-native').textContent = labels.ORIGINAL;
    document.getElementById('lb-close').textContent = labels.CLOSE;
    document.querySelector('#lb .hint').textContent = labels.HINT;
    document.getElementById('lb').setAttribute('aria-label', labels.VIEWER);
    side.setAttribute('aria-label', labels.CONTENTS);
    burger.setAttribute('aria-label', labels.CONTENTS);
    q.placeholder = labels.FILTER;
    q.setAttribute('aria-label', labels.FILTER);
    languageControl.setAttribute('aria-label', labels.LANGUAGE);
    document.getElementById('avatar').alt = labels.MASCOT;
    languageOptions.forEach(function (a) {
      var selected = a.lang === activeLanguage;
      a.id = selected ? 'language-current' : 'language-link';
      if (selected) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    });
  }

  function takeChildren(element) {
    var fragment = document.createDocumentFragment();
    while (element.firstChild) fragment.appendChild(element.firstChild);
    return fragment;
  }

  function preparePage(language) {
    var page = pages[language];
    if (page.mainNodes) return page;
    var template = document.createElement('template');
    template.innerHTML = page.mainHTML;
    template.content.querySelectorAll('img[data-source]').forEach(function (img) {
      var source = assets[img.dataset.source];
      if (!source) throw new Error('Missing embedded image: ' + img.dataset.source);
      img.src = source;
    });
    page.mainNodes = template.content;
    var nav = document.createElement('template');
    nav.innerHTML = page.tocHTML;
    page.tocNodes = nav.content;
    delete page.mainHTML;
    delete page.tocHTML;
    return page;
  }

  function warmPictures(fragment, id) {
    var anchor = fragment.getElementById(id) || fragment.getElementById('cover');
    var pictures = [], node = anchor;
    for (var i = 0; node && i < 8 && pictures.length < 4; i++, node = node.nextElementSibling) {
      pictures.push.apply(pictures, Array.from(node.querySelectorAll('img')).slice(0, 4 - pictures.length));
      if (node !== anchor && /^H[1-6]$/.test(node.tagName)) break;
    }
    return Promise.all(pictures.map(function (img) { return img.decode().catch(function () {}); }));
  }

  function readingPosition() {
    updateReading();
    if (!current) return {id: 'cover', translated: 'cover', fraction: 0, offset: 0};
    var index = heads.indexOf(current), edge = readingEdge(), rect = current.getBoundingClientRect();
    var nextTop = heads[index + 1] ? heads[index + 1].getBoundingClientRect().top : root.scrollHeight - scrollY;
    return {
      id: current.id, translated: current.dataset.translationId,
      fraction: Math.max(0, Math.min(1, (edge - rect.top) / Math.max(1, nextTop - rect.top))),
      offset: Math.max(0, rect.top - edge),
    };
  }

  function moveToSection(id, position) {
    var anchor = document.getElementById(id) || document.getElementById('cover');
    if (anchor.matches('details.source-file')) anchor.open = true;
    var edge = readingEdge();
    var y = scrollY + anchor.getBoundingClientRect().top - edge;
    if (position && id !== 'cover') {
      var index = heads.indexOf(anchor);
      var nextY = heads[index + 1] ? scrollY + heads[index + 1].getBoundingClientRect().top : root.scrollHeight;
      y += position.fraction * Math.max(1, nextY - (y + edge)) - position.offset;
    }
    window.scrollTo({top: id === 'cover' ? 0 : Math.max(0, y), behavior: 'instant'});
    updateReading();
  }

  function commitPage(language, id, position, push) {
    if (push && position) {
      history.replaceState({readerSection: position.id, readerPosition: position}, '', location.href);
    }
    if (language !== activeLanguage) {
      var page = pages[language], previous = pages[activeLanguage];
      previous.mainNodes = takeChildren(main);
      previous.tocNodes = takeChildren(toc);
      main.appendChild(page.mainNodes);
      toc.appendChild(page.tocNodes);
      activeLanguage = language;
      side.classList.remove('open');
      burger.setAttribute('aria-expanded', 'false');
      q.value = '';
      updateLabels();
      bindReader();
    }
    if (push) {
      var url = new URL(location.href);
      url.searchParams.set('lang', language);
      url.hash = id;
      history.pushState({readerSection: id, readerPosition: position}, '', url);
    }
    moveToSection(id, position);
    root.classList.remove('initial-language');
  }

  async function switchLanguage(language, id, options) {
    var token = ++switchToken;
    if (transition) transition.skipTransition();
    busy = true;
    languageControl.setAttribute('aria-busy', 'true');
    languageControl.dataset.activeLanguage = language;
    try {
      if (language !== activeLanguage) {
        var page = preparePage(language);
        if (options.animate && !reducedMotion.matches) {
          var duration = parseFloat(getComputedStyle(root).getPropertyValue('--switch-duration')) || 220;
          // The selector finishes once, with the old article still readable. It never resets on a navigation.
          await Promise.all([warmPictures(page.mainNodes, id), new Promise(function (resolve) { setTimeout(resolve, duration); })]);
        }
      }
      if (token !== switchToken) return;
      var update = function () { commitPage(language, id, options.position, options.push); };
      if (options.animate && !reducedMotion.matches && document.startViewTransition) {
        transition = document.startViewTransition(update);
        await transition.finished;
      } else {
        update();
        if (options.animate && !reducedMotion.matches) {
          await main.animate([{opacity: 0.65}, {opacity: 1}], {duration: 180, easing: 'ease-out'}).finished;
        }
      }
    } catch (error) {
      // Keep the original static links usable if a browser cannot enhance this document.
      if (token === switchToken && options.push) {
        location.assign(encodeURI(pages[language].file) + '#' + encodeURIComponent(id));
      }
    } finally {
      if (token === switchToken) {
        busy = false;
        transition = null;
        languageControl.dataset.activeLanguage = activeLanguage;
        languageControl.removeAttribute('aria-busy');
        root.classList.remove('initial-language');
      }
    }
  }

  languageControl.addEventListener('click', function (event) {
    var option = event.target.closest('a[lang]');
    if (!option || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (busy || option.lang === activeLanguage) return;
    var position = readingPosition();
    switchLanguage(option.lang, position.translated, {animate: true, push: true, position: position});
  });

  function restoreRoute() {
    var requested = new URL(location.href).searchParams.get('lang');
    var language = requested === 'zh-CN' || requested === 'en-US' ? requested : initialLanguage;
    var id;
    try { id = decodeURIComponent(location.hash.slice(1)) || 'cover'; }
    catch (_) { id = 'cover'; }
    var state = history.state || {};
    id = state.readerSection || id;
    if (language !== activeLanguage || busy) {
      switchLanguage(language, id, {animate: false, push: false, position: state.readerPosition});
    } else {
      root.classList.remove('initial-language');
      moveToSection(id, state.readerPosition);
    }
  }

  q.addEventListener('input', filterContents);
  burger.onclick = function () {
    var open = side.classList.toggle('open');
    burger.setAttribute('aria-expanded', String(open));
  };
  side.addEventListener('click', function (event) {
    if (event.target.closest('a')) {
      side.classList.remove('open');
      burger.setAttribute('aria-expanded', 'false');
    }
  });
  document.getElementById('theme').onclick = function () {
    var theme = root.dataset.theme === 'light' ? 'dark' : 'light';
    root.dataset.theme = theme;
    try { localStorage.setItem('d3dx-tut-theme', theme); } catch (_) {}
  };
  window.addEventListener('scroll', queueReading, {passive: true});
  window.addEventListener('resize', queueReading);
  window.addEventListener('hashchange', function () {
    var id;
    try { id = decodeURIComponent(location.hash.slice(1)); }
    catch (_) { id = ''; }
    var source = document.getElementById(id);
    if (source && source.matches('details.source-file')) {
      source.open = true;
      source.scrollIntoView();
    }
    queueReading();
  });
  window.addEventListener('load', queueReading);
  window.addEventListener('popstate', restoreRoute);
  reducedMotion.addEventListener('change', function () {
    if (reducedMotion.matches && transition) transition.skipTransition();
  });

  // The lightbox is shared by both cached editions. Images retain their original lossless pixels.
  var lb = document.getElementById('lb'), lbi = lb.querySelector('img');
  var stage = document.getElementById('lb-stage'), previousFocus = null, oldOverflow = '';
  var fitButton = document.getElementById('lb-fit'), nativeButton = document.getElementById('lb-native');
  function fitPicture() {
    lb.classList.remove('native');
    function sizeToStage() {
      if (!lb.classList.contains('on') || lb.classList.contains('native') || !lbi.naturalWidth) return;
      var scale = Math.min(stage.clientWidth / lbi.naturalWidth, stage.clientHeight / lbi.naturalHeight, 1 / (window.devicePixelRatio || 1));
      lbi.style.width = (lbi.naturalWidth * scale) + 'px';
    }
    sizeToStage();
    if (!lbi.complete) lbi.decode().then(sizeToStage).catch(function () {});
    stage.scrollTo(0, 0);
    nativeButton.setAttribute('aria-pressed', 'false');
  }
  function originalPicture() {
    lb.classList.add('native');
    function sizeToPixels() {
      if (lb.classList.contains('on') && lb.classList.contains('native') && lbi.naturalWidth) {
        lbi.style.width = (lbi.naturalWidth / (window.devicePixelRatio || 1)) + 'px';
      }
    }
    sizeToPixels();
    if (!lbi.complete) lbi.decode().then(sizeToPixels).catch(function () {});
    stage.scrollTo(0, 0);
    nativeButton.setAttribute('aria-pressed', 'true');
  }
  function closePicture() {
    if (!lb.classList.contains('on')) return;
    lb.classList.remove('on', 'native');
    lb.setAttribute('aria-hidden', 'true');
    lbi.removeAttribute('src');
    document.body.style.overflow = oldOverflow;
    wrap.inert = top.inert = reading.inert = false;
    if (previousFocus) previousFocus.focus({preventScroll: true});
  }
  function bindPictures() {
    main.querySelectorAll('figure img,#cover img,.image-link img,.emote').forEach(function (img) {
      var imageLink = img.closest('a.image-link'), trigger = imageLink || img;
      trigger.tabIndex = 0;
      trigger.setAttribute('role', 'button');
      function openPicture(event) {
        if (event) event.preventDefault();
        previousFocus = trigger;
        oldOverflow = document.body.style.overflow;
        lbi.src = imageLink ? imageLink.href : img.src;
        lbi.alt = img.alt;
        lb.classList.add('on');
        fitPicture();
        lb.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
        wrap.inert = top.inert = reading.inert = true;
        fitButton.focus();
      }
      trigger.onclick = openPicture;
      trigger.onkeydown = function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openPicture(); }
      };
    });
  }
  fitButton.onclick = fitPicture;
  nativeButton.onclick = originalPicture;
  document.getElementById('lb-close').onclick = closePicture;
  window.addEventListener('resize', function () {
    if (lb.classList.contains('on')) lb.classList.contains('native') ? originalPicture() : fitPicture();
  });
  lbi.onclick = function () { lb.classList.contains('native') ? fitPicture() : originalPicture(); };
  lb.onclick = function (event) { if (event.target === lb || event.target === stage) closePicture(); };
  lb.addEventListener('keydown', function (event) {
    if (event.key !== 'Tab') return;
    var buttons = Array.from(lb.querySelectorAll('button'));
    if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons[buttons.length - 1].focus(); }
    else if (!event.shiftKey && document.activeElement === buttons[buttons.length - 1]) { event.preventDefault(); buttons[0].focus(); }
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      closePicture();
      side.classList.remove('open');
      burger.setAttribute('aria-expanded', 'false');
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && !lb.classList.contains('on')) {
      event.preventDefault();
      q.focus();
      q.select();
      if (innerWidth <= 900) { side.classList.add('open'); burger.setAttribute('aria-expanded', 'true'); }
    }
  });
  bindReader();
  restoreRoute();
})();
