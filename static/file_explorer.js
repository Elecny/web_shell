class FileExplorer {
    constructor(container, options = {}) {
        this.container = container;
        this.rootPath = options.rootPath;
        this.onOpenFile = options.onOpenFile || (() => {});
        this.activeElement = null;
        this.contextMenu = null;
        this._entryCache = new Map();
        this._refreshId = 0;

        this.initLayout();
        this._init();
    }

    async _init() {
        try {
            const resp = await fetch('/api/rootpath');
            const data = await resp.json();
            if (data.rootPath) {
                this.rootPath = data.rootPath;
            }
        } catch (e) {
            console.warn('Failed to fetch rootpath:', e);
        }
        this.buildBreadcrumb(this.rootPath);
        this.loadDirectory(this.rootPath, this.treeEl, true);

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) this.refresh();
        });
    }

    initLayout() {
        this.container.innerHTML = `
            <div class="fe-header">
                <span class="fe-title">EXPLORER</span>
            </div>
            <div class="fe-breadcrumb"></div>
            <div class="fe-tree"></div>
            <div class="fe-status-bar"></div>
        `;
        this.treeEl = this.container.querySelector('.fe-tree');
        this.breadcrumbEl = this.container.querySelector('.fe-breadcrumb');
        this.statusBar = this.container.querySelector('.fe-status-bar');

        this.container.addEventListener('contextmenu', (e) => {
            const item = e.target.closest('.fe-item');
            if (!item) return;
            e.preventDefault();
            this.showContextMenu(e.clientX, e.clientY, item);
        });

        this.treeEl.addEventListener('keydown', (e) => {
            if (e.key === 'r' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                this.refresh();
            }
        });

        this.treeEl.addEventListener('click', (e) => {
            const item = e.target.closest('.fe-item');
            if (!item) return;
            e.stopPropagation();
            this.selectItem(item);
            if (item.dataset.isDir === 'true') {
                this._toggleDir(item);
            } else {
                this.onOpenFile(item.dataset.path);
            }
        });

        this.treeEl.addEventListener('dblclick', (e) => {
            const item = e.target.closest('.fe-item');
            if (!item || item.dataset.isDir === 'true') return;
            e.stopPropagation();
            if (!item.classList.contains('fe-renaming')) {
                this.startRename(item);
            }
        });

        document.addEventListener('click', () => this.hideContextMenu());
    }

    _toggleDir(item) {
        const children = item.querySelector(':scope > .fe-children');
        const chevron = item.querySelector(':scope > .fe-chevron');
        const icon = item.querySelector(':scope > .fe-icon');
        if (children.classList.contains('fe-open')) {
            children.classList.remove('fe-open');
            if (chevron) chevron.textContent = '\u{25B6}';
            if (icon) icon.textContent = '\u{1F4C1}';
        } else {
            if (chevron) chevron.textContent = '\u{25BC}';
            if (icon) icon.textContent = '\u{1F4C2}';
            if (!children.dataset.loaded) {
                this.loadDirectory(item.dataset.path, children);
            }
            children.classList.add('fe-open');
        }
        this.buildBreadcrumb(item.dataset.path);
    }

    setStatus(msg) {
        this.statusBar.textContent = msg;
        clearTimeout(this._statusTimer);
        this._statusTimer = setTimeout(() => { this.statusBar.textContent = ''; }, 3000);
    }

    buildBreadcrumb(dirPath) {
        this.breadcrumbEl.replaceChildren();
        if (!dirPath) return;
        const parts = dirPath.startsWith(this.rootPath) ? dirPath.slice(this.rootPath.length).split('/').filter(Boolean) : [];
        if (parts.length === 0) {
            const name = this.rootPath.split('/').pop();
            const seg = document.createElement('span');
            seg.className = 'fe-crumb active';
            seg.textContent = name;
            this.breadcrumbEl.appendChild(seg);
            return;
        }
        let acc = this.rootPath;
        const name = this.rootPath.split('/').pop();
        const rootSeg = document.createElement('span');
        rootSeg.className = 'fe-crumb';
        rootSeg.textContent = name;
        rootSeg.dataset.path = this.rootPath;
        rootSeg.addEventListener('click', () => this._crumbClick(this.rootPath));
        this.breadcrumbEl.appendChild(rootSeg);
        for (let i = 0; i < parts.length; i++) {
            acc += '/' + parts[i];
            const crumbPath = acc;
            const sep = document.createElement('span');
            sep.className = 'fe-crumb-sep';
            sep.textContent = '/';
            this.breadcrumbEl.appendChild(sep);
            const seg = document.createElement('span');
            seg.className = 'fe-crumb' + (i === parts.length - 1 ? ' active' : '');
            seg.textContent = parts[i];
            if (i < parts.length - 1) {
                seg.dataset.path = crumbPath;
                seg.addEventListener('click', () => this._crumbClick(crumbPath));
            }
            this.breadcrumbEl.appendChild(seg);
        }
    }

    _crumbClick(targetPath) {
        const dir = this.treeEl.querySelector(`.fe-dir[data-path="${CSS.escape(targetPath)}"]`);
        if (dir) {
            dir.click();
        } else {
            if (!targetPath.startsWith(this.rootPath)) return;
            const childParts = targetPath.slice(this.rootPath.length).split('/').filter(Boolean);
            let currentEl = this.treeEl;
            let acc = this.rootPath;
            for (const p of childParts) {
                acc += '/' + p;
                const found = currentEl.querySelector(`.fe-dir[data-path="${CSS.escape(acc)}"]`);
                if (found) found.click();
                else break;
                currentEl = found.querySelector('.fe-children');
                if (!currentEl) break;
            }
        }
    }

    async loadDirectory(dirPath, parentEl, isRoot) {
        try {
            parentEl.replaceChildren();
            const spinner = document.createElement('div');
            spinner.className = 'fe-spinner';
            const ring = document.createElement('div');
            ring.className = 'fe-spinner-ring';
            spinner.appendChild(ring);
            parentEl.appendChild(spinner);
            parentEl.dataset.path = dirPath;
            parentEl.dataset.loaded = 'true';

            const resp = await fetch(`/api/files?path=${encodeURIComponent(dirPath)}`);
            const data = await resp.json();
            if (data.error) throw new Error(data.error);

            const fragment = document.createDocumentFragment();
            for (const entry of data.entries) {
                if (this._entryCache.size > 5000) {
                    const first = this._entryCache.keys().next().value;
                    this._entryCache.delete(first);
                }
                this._entryCache.set(entry.path, entry);
                fragment.appendChild(this.createItem(entry));
            }
            if (data.entries.length === 0) {
                const empty = document.createElement('div');
                empty.className = 'fe-empty';
                empty.textContent = '(empty)';
                fragment.appendChild(empty);
            }
            parentEl.replaceChildren(fragment);
        } catch (err) {
            parentEl.replaceChildren();
            const errDiv = document.createElement('div');
            errDiv.className = 'fe-error';
            errDiv.textContent = err.message;
            parentEl.appendChild(errDiv);
        }
    }

    async refresh() {
        const id = ++this._refreshId;
        const openPaths = [];
        this.treeEl.querySelectorAll('.fe-children.fe-open').forEach(el => openPaths.push(el.dataset.path));
        this.buildBreadcrumb(this.rootPath);
        await this.loadDirectory(this.rootPath, this.treeEl, true);
        if (id !== this._refreshId) return;
        for (const p of openPaths) {
            const child = this.treeEl.querySelector(`.fe-children[data-path="${CSS.escape(p)}"]`);
            if (child) {
                const dirEl = child.closest('.fe-dir');
                if (dirEl) {
                    const chevron = dirEl.querySelector('.fe-chevron');
                    if (chevron) chevron.textContent = '\u{25BC}';
                    const icon = dirEl.querySelector('.fe-icon');
                    if (icon) icon.textContent = '\u{1F4C2}';
                    await this.loadDirectory(p, child);
                    if (id !== this._refreshId) return;
                    child.classList.add('fe-open');
                }
            }
        }
        this.setStatus('Refreshed');
    }

    createItem(entry) {
        const div = document.createElement('div');
        div.className = 'fe-item' + (entry.is_dir ? ' fe-dir' : ' fe-file');
        div.dataset.path = entry.path;
        div.dataset.isDir = entry.is_dir ? 'true' : 'false';
        div.tabIndex = 0;

        if (entry.is_dir) {
            const chevron = document.createElement('span');
            chevron.className = 'fe-chevron';
            chevron.textContent = '\u{25B6}';
            div.appendChild(chevron);

            div.appendChild(this._makeIcon(entry));

            const nameSpan = document.createElement('span');
            nameSpan.className = 'fe-name';
            nameSpan.textContent = entry.name;
            div.appendChild(nameSpan);

            const children = document.createElement('div');
            children.className = 'fe-children';
            children.dataset.path = entry.path;
            div.appendChild(children);
        } else {
            div.appendChild(this._makeIcon(entry));

            const nameSpan = document.createElement('span');
            nameSpan.className = 'fe-name';
            nameSpan.textContent = entry.name;
            div.appendChild(nameSpan);

            const sizeSpan = document.createElement('span');
            sizeSpan.className = 'fe-size';
            sizeSpan.textContent = this._formatSize(entry.size);
            div.appendChild(sizeSpan);
        }

        return div;
    }

    _makeIcon(entry) {
        const icon = document.createElement('span');
        icon.className = 'fe-icon';
        if (entry.is_dir) {
            icon.textContent = '\u{1F4C1}';
        } else {
            const ext = entry.name.includes('.') ? entry.name.split('.').pop().toLowerCase() : '';
            icon.textContent = FILE_SYMBOLS[ext] || '\u{1F4C4}';
            if (FILE_EXT_COLORS[ext]) icon.style.color = FILE_EXT_COLORS[ext];
        }
        return icon;
    }

    _formatSize(bytes) {
        if (bytes === 0) return '';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    selectItem(item) {
        if (this.activeElement) {
            this.activeElement.classList.remove('fe-selected');
        }
        this.activeElement = item;
        item.classList.add('fe-selected');
        item.focus({preventScroll: true});
    }

    _getParentPath(item) {
        const parent = item.parentElement;
        if (parent && parent.dataset && parent.dataset.path) return parent.dataset.path;
        const dir = item.closest('.fe-dir');
        return dir ? dir.dataset.path : this.rootPath;
    }

    async action_new_file() {
        const parentPath = this.activeElement
            ? (this.activeElement.dataset.isDir === 'true'
                ? this.activeElement.dataset.path
                : this._getParentPath(this.activeElement))
            : this.rootPath;
        const name = prompt('File name:');
        if (!name) return;
        try {
            const resp = await fetch('/api/files/write', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: parentPath + '/' + name, content: ''}),
            });
            const data = await resp.json();
            if (data.error) throw new Error(data.error);
            const parentEl = this.treeEl.querySelector(`.fe-children[data-path="${CSS.escape(parentPath)}"]`)
                || this.treeEl;
            await this.loadDirectory(parentPath, parentEl);
            this.setStatus('Created ' + name);
        } catch (err) {
            this.setStatus('Error: ' + err.message);
        }
    }

    async action_new_folder() {
        const parentPath = this.activeElement
            ? (this.activeElement.dataset.isDir === 'true'
                ? this.activeElement.dataset.path
                : this._getParentPath(this.activeElement))
            : this.rootPath;
        const name = prompt('Folder name:');
        if (!name) return;
        try {
            const resp = await fetch('/api/files/mkdir', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: parentPath + '/' + name}),
            });
            const data = await resp.json();
            if (data.error) throw new Error(data.error);
            const parentEl = this.treeEl.querySelector(`.fe-children[data-path="${CSS.escape(parentPath)}"]`)
                || this.treeEl;
            await this.loadDirectory(parentPath, parentEl);
            this.setStatus('Created ' + name);
        } catch (err) {
            this.setStatus('Error: ' + err.message);
        }
    }

    action_collapse_all() {
        this.treeEl.querySelectorAll('.fe-children.fe-open').forEach(el => {
            el.classList.remove('fe-open');
            const dirEl = el.closest('.fe-dir');
            if (dirEl) {
                const chevron = dirEl.querySelector('.fe-chevron');
                if (chevron) chevron.textContent = '\u{25B6}';
                const icon = dirEl.querySelector('.fe-icon');
                if (icon) icon.textContent = '\u{1F4C1}';
            }
        });
        this.buildBreadcrumb(this.rootPath);
        this.setStatus('Collapsed all');
    }

    action_refresh() { this.refresh(); }

    startRename(item) {
        const nameSpan = item.querySelector('.fe-name');
        const oldName = nameSpan.textContent;
        const input = document.createElement('input');
        input.className = 'fe-rename-input';
        input.value = oldName;
        const dot = oldName.lastIndexOf('.');
        if (dot > 0) input.setSelectionRange(0, dot);
        else input.setSelectionRange(0, oldName.length);
        nameSpan.replaceChildren(input);
        item.classList.add('fe-renaming');
        input.focus();

        let finished = false;
        const finish = async () => {
            if (finished) return;
            finished = true;
            const newName = input.value.trim();
            if (newName && newName !== oldName) {
                const parentPath = this._getParentPath(item);
                try {
                    const resp = await fetch('/api/files/rename', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({path: parentPath + '/' + oldName, newPath: parentPath + '/' + newName}),
                    });
                    const data = await resp.json();
                    if (data.error) throw new Error(data.error);
                    const parentEl = item.parentElement;
                    await this.loadDirectory(parentPath, parentEl);
                    this.setStatus('Renamed to ' + newName);
                } catch (err) {
                    this.setStatus('Error: ' + err.message);
                }
            } else {
                nameSpan.textContent = oldName;
            }
            item.classList.remove('fe-renaming');
        };

        input.addEventListener('blur', finish);
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') input.blur();
            if (e.key === 'Escape') {
                nameSpan.textContent = oldName;
                item.classList.remove('fe-renaming');
            }
        });
    }

    showContextMenu(x, y, item) {
        this.hideContextMenu();
        this.selectItem(item);

        const menu = document.createElement('div');
        menu.className = 'fe-context-menu';
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        menu.style.left = Math.min(x, vw - 180) + 'px';
        menu.style.top = Math.min(y, vh - 200) + 'px';

        const isDir = item.dataset.isDir === 'true';
        const groups = [
            [
                {label: 'New File', action: 'new-file'},
                {label: 'New Folder', action: 'new-folder'},
            ],
            [
                {label: 'Open', action: 'open'},
                {label: 'Rename', action: 'rename'},
                {label: 'Delete', action: 'delete'},
            ],
            [{label: 'Refresh', action: 'refresh'}],
        ];

        for (const group of groups) {
            for (const a of group) {
                const d = document.createElement('div');
                d.className = 'fe-context-item';
                const ls = document.createElement('span');
                ls.textContent = a.label;
                d.appendChild(ls);
                if (a.label === 'Delete') d.classList.add('fe-context-danger');
                d.addEventListener('click', (e) => {
                    e.stopPropagation();
                    this.hideContextMenu();
                    this['context_' + a.action](item);
                });
                menu.appendChild(d);
            }
            const sep = document.createElement('div');
            sep.className = 'fe-context-sep';
            menu.appendChild(sep);
        }

        this.container.appendChild(menu);
        this.contextMenu = menu;
    }

    hideContextMenu() {
        if (this.contextMenu) {
            this.contextMenu.remove();
            this.contextMenu = null;
        }
    }

    context_new_file(item) { this.action_new_file(); }
    context_new_folder(item) { this.action_new_folder(); }
    context_open(item) {
        if (item.dataset.isDir === 'true') item.click();
        else this.onOpenFile(item.dataset.path);
    }
    context_rename(item) { this.startRename(item); }
    context_refresh() { this.refresh(); }

    async context_delete(item) {
        if (!confirm('Delete "' + (item.querySelector('.fe-name')?.textContent || item.dataset.path) + '"?')) return;
        try {
            const resp = await fetch('/api/files/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: item.dataset.path}),
            });
            const data = await resp.json();
            if (data.error) throw new Error(data.error);
            const parentPath = this._getParentPath(item);
            const parentEl = this.treeEl.querySelector(`.fe-children[data-path="${CSS.escape(parentPath)}"]`)
                || this.treeEl;
            await this.loadDirectory(parentPath, parentEl);
            this.setStatus('Deleted');
        } catch (err) {
            this.setStatus('Error: ' + err.message);
        }
    }
}

const FILE_SYMBOLS = {
    js: '\u{1F7E8}', jsx: '\u{1F7E8}', ts: '\u{1F7E7}', tsx: '\u{1F7E7}',
    py: '\u{1F7E9}', html: '\u{1F534}', css: '\u{1F7E6}',
    json: '\u{2699}\u{FE0F}', md: '\u{1F4DD}', txt: '\u{1F4C4}',
    sh: '\u{1F4BB}', bash: '\u{1F4BB}', fish: '\u{1F4BB}',
    yml: '\u{2699}\u{FE0F}', yaml: '\u{2699}\u{FE0F}', toml: '\u{2699}\u{FE0F}',
    png: '\u{1F5BC}\u{FE0F}', jpg: '\u{1F5BC}\u{FE0F}', svg: '\u{1F5BC}\u{FE0F}',
    zip: '\u{1F4E6}', tar: '\u{1F4E6}', gz: '\u{1F4E6}',
    log: '\u{1F4DD}', cfg: '\u{2699}\u{FE0F}',
    gitignore: '\u{1F4C1}',
};

const FILE_EXT_COLORS = {
    js: '#f9e2af', jsx: '#f9e2af', ts: '#89b4fa', tsx: '#89b4fa',
    py: '#a6e3a1', rb: '#f38ba8', rs: '#fab387', go: '#89dceb',
    java: '#f38ba8', kt: '#fab387', scala: '#f38ba8',
    html: '#f38ba8', css: '#89b4fa', scss: '#f9e2af', less: '#f9e2af',
    json: '#f9e2af', xml: '#f9e2af', yml: '#f9e2af', yaml: '#f9e2af', toml: '#f9e2af',
    md: '#89dceb', txt: '#a6adc8', rtf: '#a6adc8',
    sh: '#a6e3a1', bash: '#a6e3a1', zsh: '#a6e3a1', fish: '#a6e3a1',
    png: '#cba6f7', jpg: '#fab387', jpeg: '#fab387', gif: '#cba6f7', svg: '#f9e2af',
    zip: '#fab387', tar: '#fab387', gz: '#fab387', bz2: '#fab387', '7z': '#fab387',
    log: '#a6adc8', cfg: '#a6adc8', conf: '#a6adc8', ini: '#a6adc8',
    env: '#a6e3a1', gitignore: '#a6adc8',
};
