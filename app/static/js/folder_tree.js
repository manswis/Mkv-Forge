/* Pure lazy-loaded folder tree state and visible-row flattener for the Save As picker. */

(function attachFolderTreeHelpers(global) {
  const { FOLDER_TREE } = global.MKVForgeConstants;

  function normalizeFolderPath(rawPath) {
    const cleaned = String(rawPath || "").trim().replace(/^\/+|\/+$/g, "");
    if (!cleaned) return FOLDER_TREE.ROOT_PATH;
    return `/${cleaned}`;
  }

  function getAncestorPaths(rawPath) {
    const normalized = normalizeFolderPath(rawPath);
    if (normalized === FOLDER_TREE.ROOT_PATH) {
      return [FOLDER_TREE.ROOT_PATH];
    }
    const segments = normalized.slice(1).split("/").filter(Boolean);
    const ancestors = [FOLDER_TREE.ROOT_PATH];
    let current = "";
    segments.forEach((seg) => {
      current = `${current}/${seg}`;
      ancestors.push(current);
    });
    return ancestors;
  }

  function createFolderTreeState(initialPath) {
    const selectedPath = normalizeFolderPath(initialPath);
    const expanded = {};
    const ancestors = getAncestorPaths(selectedPath);
    // Expand root and all parent folders leading to selectedPath
    ancestors.slice(0, -1).forEach((ancestor) => {
      expanded[ancestor] = true;
    });
    expanded[FOLDER_TREE.ROOT_PATH] = true;

    return {
      selectedPath,
      expanded,
      loading: {},
      childrenByPath: {},
      loadedPaths: {},
      error: null,
      newFolderOpen: false,
      newFolderName: "",
      creatingFolder: false,
    };
  }

  function mergeFolderChildren(treeState, parentPath, entries) {
    const normalizedParent = normalizeFolderPath(parentPath);
    const sorted = Array.isArray(entries)
      ? [...entries].sort((a, b) =>
          String(a.name || "").localeCompare(String(b.name || ""), undefined, {
            sensitivity: "base",
          })
        )
      : [];

    treeState.childrenByPath = {
      ...treeState.childrenByPath,
      [normalizedParent]: sorted,
    };
    treeState.loadedPaths = {
      ...treeState.loadedPaths,
      [normalizedParent]: true,
    };

    // Update parent node's has_children badge if it lives under a loaded grandparent
    if (normalizedParent !== FOLDER_TREE.ROOT_PATH) {
      const idx = normalizedParent.lastIndexOf("/");
      const grandparent = idx <= 0 ? FOLDER_TREE.ROOT_PATH : normalizedParent.slice(0, idx);
      const siblings = treeState.childrenByPath[grandparent];
      if (Array.isArray(siblings)) {
        treeState.childrenByPath = {
          ...treeState.childrenByPath,
          [grandparent]: siblings.map((entry) =>
            entry.path === normalizedParent
              ? { ...entry, has_children: sorted.length > 0 }
              : entry
          ),
        };
      }
    }
  }

  function flattenFolderTree(treeState) {
    if (!treeState) return [];
    const rows = [];
    const rootChildren = treeState.childrenByPath[FOLDER_TREE.ROOT_PATH] || [];
    const rootHasChildren =
      !treeState.loadedPaths[FOLDER_TREE.ROOT_PATH] || rootChildren.length > 0;

    rows.push({
      path: FOLDER_TREE.ROOT_PATH,
      name: FOLDER_TREE.ROOT_LABEL,
      depth: 0,
      isRoot: true,
      hasChildren: rootHasChildren,
      isExpanded: Boolean(treeState.expanded[FOLDER_TREE.ROOT_PATH]),
      isLoading: Boolean(treeState.loading[FOLDER_TREE.ROOT_PATH]),
      isSelected: treeState.selectedPath === FOLDER_TREE.ROOT_PATH,
    });

    function walk(parentPath, depth) {
      if (!treeState.expanded[parentPath]) return;
      const children = treeState.childrenByPath[parentPath] || [];
      children.forEach((child) => {
        const path = normalizeFolderPath(child.path);
        const isExpanded = Boolean(treeState.expanded[path]);
        const isLoading = Boolean(treeState.loading[path]);
        rows.push({
          path,
          name: child.name,
          depth,
          isRoot: false,
          hasChildren: Boolean(child.has_children),
          isExpanded,
          isLoading,
          isSelected: treeState.selectedPath === path,
        });
        if (isExpanded) {
          walk(path, depth + 1);
        }
      });
    }

    walk(FOLDER_TREE.ROOT_PATH, 1);
    return rows;
  }

  function rowIndentStyle(depth) {
    const rem =
      FOLDER_TREE.BASE_INDENT_REM +
      Math.max(0, Number(depth || 0)) * FOLDER_TREE.INDENT_STEP_REM;
    return `padding-left: ${rem.toFixed(2)}rem`;
  }

  function buildBreadcrumbs(
    rawPath,
    { includeRoot = true, rootLabel = FOLDER_TREE.BREADCRUMB_ROOT_LABEL } = {}
  ) {
    const normalized = normalizeFolderPath(rawPath);
    const crumbs = [];
    if (includeRoot || normalized === FOLDER_TREE.ROOT_PATH) {
      crumbs.push({ name: rootLabel, path: FOLDER_TREE.ROOT_PATH });
    }
    if (normalized === FOLDER_TREE.ROOT_PATH) {
      return crumbs;
    }
    const segments = normalized.slice(1).split("/").filter(Boolean);
    let acc = "";
    segments.forEach((seg) => {
      acc = `${acc}/${seg}`;
      crumbs.push({ name: seg, path: acc });
    });
    return crumbs;
  }

  function getParentFolderName(
    rawPath,
    rootLabel = FOLDER_TREE.BREADCRUMB_ROOT_LABEL
  ) {
    const normalized = normalizeFolderPath(rawPath);
    if (normalized === FOLDER_TREE.ROOT_PATH) {
      return rootLabel;
    }
    const segments = normalized.slice(1).split("/").filter(Boolean);
    return segments.length > 0 ? segments[segments.length - 1] : rootLabel;
  }

  global.MKVForgeFolderTree = Object.freeze({
    normalizeFolderPath,
    getAncestorPaths,
    buildBreadcrumbs,
    getParentFolderName,
    createFolderTreeState,
    mergeFolderChildren,
    flattenFolderTree,
    rowIndentStyle,
  });
})(window);
