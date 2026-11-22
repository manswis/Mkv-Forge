/**
 * MKV Forge — Pure SVG Pie / Annular Sector Chart Geometry Engine
 * 100% offline, zero external dependencies, deterministic & unit-testable.
 */
(function (global) {
  "use strict";

  const CHART_CONFIG = Object.freeze({
    SVG_NS: "http://www.w3.org/2000/svg",
    VIEWBOX_SIZE: 200,
    CENTER: 100,
    INNER_RADIUS: 46,
    OUTER_RADIUS: 82,
    ACTIVE_OUTER_RADIUS: 91,
    ACTIVE_POP_OFFSET: 5,
    START_ANGLE_DEG: -90,
    FULL_CIRCLE_DEG: 360,
    HALF_CIRCLE_DEG: 180,
    FULL_CIRCLE_EPSILON_DEG: 0.05,
    PAD_ANGLE_DEG: 2.2,
    MAX_PAD_FRACTION: 0.25,
    MIN_LABEL_PERCENT: 7.5,
    PERCENT_MULTIPLIER: 100,
    PERCENT_ROUND_FACTOR: 10,
    RAD_PER_DEG: Math.PI / 180,
    ACTIVE_OPACITY: "1",
    INACTIVE_OPACITY: "0.55",
    DEFAULT_COLOR_START: "#3B82F6",
    DEFAULT_COLOR_END: "#2563EB",
    LABEL_FONT_FAMILY: "ui-monospace, SFMono-Regular, Menlo, monospace",
    LABEL_FONT_SIZE: "10.5",
    LABEL_FONT_WEIGHT: "700",
    LABEL_FILL: "#FFFFFF",
    LABEL_TEXT_SHADOW: "0 1px 2px rgba(0,0,0,0.6)",
    ACTIVE_DROP_SHADOW: "drop-shadow(0 4px 10px rgba(0, 0, 0, 0.38))",
    SLICE_TRANSITION: "opacity 150ms ease",
  });

  function polarToCartesian(cx, cy, radius, angleDeg) {
    const rad = angleDeg * CHART_CONFIG.RAD_PER_DEG;
    return {
      x: cx + radius * Math.cos(rad),
      y: cy + radius * Math.sin(rad),
    };
  }

  function formatCoord(value) {
    return Number(value).toFixed(2);
  }

  /**
   * Build a full 360-degree annular ring using two 180-degree arcs so SVG never
   * collapses an arc whose start and end coordinates coincide.
   */
  function buildFullRingPath(cx, cy, innerRadius, outerRadius) {
    const outerTop = polarToCartesian(cx, cy, outerRadius, CHART_CONFIG.START_ANGLE_DEG);
    const outerBottom = polarToCartesian(
      cx,
      cy,
      outerRadius,
      CHART_CONFIG.START_ANGLE_DEG + CHART_CONFIG.HALF_CIRCLE_DEG
    );
    const innerTop = polarToCartesian(cx, cy, innerRadius, CHART_CONFIG.START_ANGLE_DEG);
    const innerBottom = polarToCartesian(
      cx,
      cy,
      innerRadius,
      CHART_CONFIG.START_ANGLE_DEG + CHART_CONFIG.HALF_CIRCLE_DEG
    );
    const rOut = formatCoord(outerRadius);
    const rIn = formatCoord(innerRadius);

    return [
      `M ${formatCoord(outerTop.x)} ${formatCoord(outerTop.y)}`,
      `A ${rOut} ${rOut} 0 1 1 ${formatCoord(outerBottom.x)} ${formatCoord(outerBottom.y)}`,
      `A ${rOut} ${rOut} 0 1 1 ${formatCoord(outerTop.x)} ${formatCoord(outerTop.y)}`,
      `M ${formatCoord(innerTop.x)} ${formatCoord(innerTop.y)}`,
      `A ${rIn} ${rIn} 0 1 0 ${formatCoord(innerBottom.x)} ${formatCoord(innerBottom.y)}`,
      `A ${rIn} ${rIn} 0 1 0 ${formatCoord(innerTop.x)} ${formatCoord(innerTop.y)}`,
      "Z",
    ].join(" ");
  }

  /**
   * Build an annular sector (donut wedge) SVG path from startDeg to endDeg.
   */
  function buildAnnularSectorPath(cx, cy, innerRadius, outerRadius, startDeg, endDeg) {
    const sweep = endDeg - startDeg;
    if (sweep <= 0) return "";
    if (sweep >= CHART_CONFIG.FULL_CIRCLE_DEG - CHART_CONFIG.FULL_CIRCLE_EPSILON_DEG) {
      return buildFullRingPath(cx, cy, innerRadius, outerRadius);
    }

    const largeArcFlag = sweep > CHART_CONFIG.HALF_CIRCLE_DEG ? 1 : 0;
    const outerStart = polarToCartesian(cx, cy, outerRadius, startDeg);
    const outerEnd = polarToCartesian(cx, cy, outerRadius, endDeg);
    const innerEnd = polarToCartesian(cx, cy, innerRadius, endDeg);
    const innerStart = polarToCartesian(cx, cy, innerRadius, startDeg);
    const rOut = formatCoord(outerRadius);
    const rIn = formatCoord(innerRadius);

    return [
      `M ${formatCoord(outerStart.x)} ${formatCoord(outerStart.y)}`,
      `A ${rOut} ${rOut} 0 ${largeArcFlag} 1 ${formatCoord(outerEnd.x)} ${formatCoord(outerEnd.y)}`,
      `L ${formatCoord(innerEnd.x)} ${formatCoord(innerEnd.y)}`,
      `A ${rIn} ${rIn} 0 ${largeArcFlag} 0 ${formatCoord(innerStart.x)} ${formatCoord(innerStart.y)}`,
      "Z",
    ].join(" ");
  }

  /**
   * Compute rich interactive pie slices (resting path, popped active path,
   * midpoint label coordinates, and rounded percentages) for a dataset.
   */
  function buildPieSlices(items, overrides) {
    if (!Array.isArray(items) || items.length === 0) return [];
    const cfg = { ...CHART_CONFIG, ...(overrides || {}) };
    const positive = items.filter((item) => Number(item && item.value) > 0);
    if (positive.length === 0) return [];

    const total = positive.reduce((sum, item) => sum + Number(item.value), 0);
    if (total <= 0) return [];

    const isMulti = positive.length > 1;
    let cursorDeg = cfg.START_ANGLE_DEG;

    return positive.map((item) => {
      const val = Number(item.value);
      const fraction = val / total;
      const rawSweep = fraction * cfg.FULL_CIRCLE_DEG;
      const pad = isMulti
        ? Math.min(cfg.PAD_ANGLE_DEG, rawSweep * cfg.MAX_PAD_FRACTION)
        : 0;
      const startDeg = cursorDeg + pad / 2;
      const endDeg = cursorDeg + rawSweep - pad / 2;
      const midDeg = cursorDeg + rawSweep / 2;
      cursorDeg += rawSweep;

      const percent =
        Math.round(fraction * cfg.PERCENT_MULTIPLIER * cfg.PERCENT_ROUND_FACTOR) /
        cfg.PERCENT_ROUND_FACTOR;
      const percentLabel = `${Math.round(percent)}%`;

      // Resting geometry
      const path = buildAnnularSectorPath(
        cfg.CENTER,
        cfg.CENTER,
        cfg.INNER_RADIUS,
        cfg.OUTER_RADIUS,
        startDeg,
        endDeg
      );
      const labelRadius = (cfg.INNER_RADIUS + cfg.OUTER_RADIUS) / 2;
      const labelPos = polarToCartesian(cfg.CENTER, cfg.CENTER, labelRadius, midDeg);

      // Active / popped-out geometry (expands outer radius & nudges radially)
      const popOffset = isMulti ? cfg.ACTIVE_POP_OFFSET : 0;
      const popCenter = polarToCartesian(cfg.CENTER, cfg.CENTER, popOffset, midDeg);
      const activePath = buildAnnularSectorPath(
        popCenter.x,
        popCenter.y,
        cfg.INNER_RADIUS,
        cfg.ACTIVE_OUTER_RADIUS,
        startDeg,
        endDeg
      );
      const activeLabelRadius = (cfg.INNER_RADIUS + cfg.ACTIVE_OUTER_RADIUS) / 2;
      const activeLabelPos = polarToCartesian(
        popCenter.x,
        popCenter.y,
        activeLabelRadius,
        midDeg
      );

      return {
        ...item,
        value: val,
        percent,
        percentLabel,
        showLabel: percent >= cfg.MIN_LABEL_PERCENT,
        path,
        activePath,
        labelX: formatCoord(labelPos.x),
        labelY: formatCoord(labelPos.y),
        activeLabelX: formatCoord(activeLabelPos.x),
        activeLabelY: formatCoord(activeLabelPos.y),
      };
    });
  }

  function escapeAttr(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /**
   * Render a complete, true-SVG-namespace interactive pie chart markup string.
   * Using a single SVG string via x-html avoids HTML5 <template> inside <svg>
   * namespace bugs across Safari/Chrome/Firefox.
   */
  function renderPieSvg(slices, activeKey, options) {
    const opts = options || {};
    const prefix = escapeAttr(opts.idPrefix || "pie");
    const ariaLabel = escapeAttr(opts.ariaLabel || "Distribution chart");
    const vb = CHART_CONFIG.VIEWBOX_SIZE;
    const center = CHART_CONFIG.CENTER;
    const list = Array.isArray(slices) ? slices : [];

    if (list.length === 0) {
      const emptyRadius = (CHART_CONFIG.INNER_RADIUS + CHART_CONFIG.OUTER_RADIUS) / 2;
      const emptyStroke = CHART_CONFIG.OUTER_RADIUS - CHART_CONFIG.INNER_RADIUS;
      return `<svg viewBox="0 0 ${vb} ${vb}" class="h-full w-full overflow-visible" role="img" aria-label="${ariaLabel}">
        <circle cx="${center}" cy="${center}" r="${formatCoord(emptyRadius)}" fill="none" stroke="currentColor" stroke-width="${formatCoord(emptyStroke)}" class="text-ink-muted/25" />
      </svg>`;
    }

    const defs = list
      .map((seg) => {
        const key = escapeAttr(seg.key);
        const cStart = escapeAttr(seg.colorStart || seg.color || "#3B82F6");
        const cEnd = escapeAttr(seg.colorEnd || seg.color || "#2563EB");
        return `<linearGradient id="${prefix}-grad-${key}" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="${cStart}" />
          <stop offset="100%" stop-color="${cEnd}" />
        </linearGradient>`;
      })
      .join("");

    const hasActive = Boolean(activeKey);
    const groups = list
      .map((seg) => {
        const key = escapeAttr(seg.key);
        const isActive = hasActive && seg.key === activeKey;
        const opacity = hasActive && !isActive ? "0.55" : "1";
        const pathD = isActive ? seg.activePath : seg.path;
        const filterStyle = isActive
          ? ' style="filter: drop-shadow(0 4px 10px rgba(0, 0, 0, 0.38));"'
          : "";
        const labelEl = seg.showLabel
          ? `<text x="${isActive ? seg.activeLabelX : seg.labelX}" y="${isActive ? seg.activeLabelY : seg.labelY}" text-anchor="middle" dominant-baseline="central" fill="#FFFFFF" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="10.5" font-weight="700" style="pointer-events:none; user-select:none; text-shadow: 0 1px 2px rgba(0,0,0,0.6);">${escapeAttr(seg.percentLabel)}</text>`
          : "";
        return `<g data-slice-key="${key}" opacity="${opacity}" style="cursor:pointer;">
          <path d="${pathD}" fill="url(#${prefix}-grad-${key})"${filterStyle} />
          ${labelEl}
        </g>`;
      })
      .join("");

    return `<svg viewBox="0 0 ${vb} ${vb}" class="h-full w-full overflow-visible" role="img" aria-label="${ariaLabel}">
      <defs>${defs}</defs>
      ${groups}
    </svg>`;
  }

  function _createSvgEl(doc, tag) {
    return doc.createElementNS(CHART_CONFIG.SVG_NS, tag);
  }

  function _findDirectChildByRole(parent, role) {
    const children = parent.children || parent.childNodes || [];
    for (let i = 0; i < children.length; i += 1) {
      const child = children[i];
      if (child && typeof child.getAttribute === "function" && child.getAttribute("data-role") === role) {
        return child;
      }
    }
    return null;
  }

  /**
   * Reconcile an interactive donut chart's SVG DOM in-place without tearing down
   * or re-mounting existing <svg>, <linearGradient>, <g>, <path>, or <text> nodes.
   * Returns true if patched successfully, false when container/document is unavailable.
   */
  function patchPieSvg(container, slices, activeKey, options) {
    if (!container || typeof container.appendChild !== "function") return false;
    const doc = container.ownerDocument || (typeof document !== "undefined" ? document : null);
    if (!doc || typeof doc.createElementNS !== "function") return false;

    const opts = options || {};
    const prefix = String(opts.idPrefix || "pie");
    const ariaLabel = String(opts.ariaLabel || "Distribution chart");
    const vb = CHART_CONFIG.VIEWBOX_SIZE;
    const center = CHART_CONFIG.CENTER;
    const list = Array.isArray(slices) ? slices : [];

    // 1. Reuse or create root <svg> once (never clear innerHTML on updates)
    let svg = null;
    const hostChildren = container.children || container.childNodes || [];
    for (let i = 0; i < hostChildren.length; i += 1) {
      const node = hostChildren[i];
      if (
        node &&
        typeof node.getAttribute === "function" &&
        node.getAttribute("data-chart-prefix") === prefix
      ) {
        svg = node;
        break;
      }
    }
    if (!svg) {
      svg = _createSvgEl(doc, "svg");
      svg.setAttribute("data-chart-prefix", prefix);
      svg.setAttribute("viewBox", `0 0 ${vb} ${vb}`);
      svg.setAttribute("class", "h-full w-full overflow-visible");
      svg.setAttribute("role", "img");
      container.appendChild(svg);
    }
    svg.setAttribute("aria-label", ariaLabel);

    // 2. Ensure persistent <defs>, <circle data-role="empty-ring">, and <g data-role="slices">
    let defsEl = _findDirectChildByRole(svg, "defs");
    if (!defsEl) {
      defsEl = _createSvgEl(doc, "defs");
      defsEl.setAttribute("data-role", "defs");
      svg.appendChild(defsEl);
    }

    let emptyRing = _findDirectChildByRole(svg, "empty-ring");
    if (!emptyRing) {
      const emptyRadius = (CHART_CONFIG.INNER_RADIUS + CHART_CONFIG.OUTER_RADIUS) / 2;
      const emptyStroke = CHART_CONFIG.OUTER_RADIUS - CHART_CONFIG.INNER_RADIUS;
      emptyRing = _createSvgEl(doc, "circle");
      emptyRing.setAttribute("data-role", "empty-ring");
      emptyRing.setAttribute("cx", String(center));
      emptyRing.setAttribute("cy", String(center));
      emptyRing.setAttribute("r", formatCoord(emptyRadius));
      emptyRing.setAttribute("fill", "none");
      emptyRing.setAttribute("stroke", "currentColor");
      emptyRing.setAttribute("stroke-width", formatCoord(emptyStroke));
      emptyRing.setAttribute("class", "text-ink-muted/25");
      svg.appendChild(emptyRing);
    }

    let slicesEl = _findDirectChildByRole(svg, "slices");
    if (!slicesEl) {
      slicesEl = _createSvgEl(doc, "g");
      slicesEl.setAttribute("data-role", "slices");
      svg.appendChild(slicesEl);
    }

    // 3. Reconcile gradients in-place
    const existingGrads = new Map();
    const defChildren = Array.from(defsEl.children || defsEl.childNodes || []);
    for (const node of defChildren) {
      if (node && typeof node.getAttribute === "function") {
        const gKey = node.getAttribute("data-grad-key");
        if (gKey) existingGrads.set(gKey, node);
      }
    }

    const activeKeys = new Set();
    for (const seg of list) {
      const key = String(seg.key);
      activeKeys.add(key);
      const cStart = String(seg.colorStart || seg.color || CHART_CONFIG.DEFAULT_COLOR_START);
      const cEnd = String(seg.colorEnd || seg.color || CHART_CONFIG.DEFAULT_COLOR_END);
      let grad = existingGrads.get(key);
      if (!grad) {
        grad = _createSvgEl(doc, "linearGradient");
        grad.setAttribute("data-grad-key", key);
        grad.setAttribute("id", `${prefix}-grad-${key}`);
        grad.setAttribute("x1", "0%");
        grad.setAttribute("y1", "0%");
        grad.setAttribute("x2", "100%");
        grad.setAttribute("y2", "100%");
        const stop0 = _createSvgEl(doc, "stop");
        stop0.setAttribute("offset", "0%");
        const stop1 = _createSvgEl(doc, "stop");
        stop1.setAttribute("offset", "100%");
        grad.appendChild(stop0);
        grad.appendChild(stop1);
        defsEl.appendChild(grad);
      }
      const stops = grad.children || grad.childNodes || [];
      if (stops[0]) stops[0].setAttribute("stop-color", cStart);
      if (stops[1]) stops[1].setAttribute("stop-color", cEnd);
    }
    for (const [oldKey, gradNode] of existingGrads.entries()) {
      if (!activeKeys.has(oldKey) && gradNode.parentNode === defsEl) {
        defsEl.removeChild(gradNode);
      }
    }

    // 4. Toggle empty ring visibility without unmounting
    emptyRing.setAttribute("display", list.length === 0 ? "" : "none");

    // 5. Reconcile slice <g>, <path>, and <text> nodes in-place
    const existingGroups = new Map();
    const sliceChildren = Array.from(slicesEl.children || slicesEl.childNodes || []);
    for (const node of sliceChildren) {
      if (node && typeof node.getAttribute === "function") {
        const sKey = node.getAttribute("data-slice-key");
        if (sKey) existingGroups.set(sKey, node);
      }
    }

    const hasActive = Boolean(activeKey);
    for (let idx = 0; idx < list.length; idx += 1) {
      const seg = list[idx];
      const key = String(seg.key);
      const isActive = hasActive && seg.key === activeKey;
      const opacity = hasActive && !isActive
        ? CHART_CONFIG.INACTIVE_OPACITY
        : CHART_CONFIG.ACTIVE_OPACITY;
      const pathD = isActive ? seg.activePath : seg.path;

      let group = existingGroups.get(key);
      let pathEl = null;
      let textEl = null;

      if (!group) {
        group = _createSvgEl(doc, "g");
        group.setAttribute("data-slice-key", key);
        if (group.style) {
          group.style.cursor = "pointer";
          group.style.transition = CHART_CONFIG.SLICE_TRANSITION;
        }
        pathEl = _createSvgEl(doc, "path");
        pathEl.setAttribute("data-role", "slice-path");
        textEl = _createSvgEl(doc, "text");
        textEl.setAttribute("data-role", "slice-label");
        textEl.setAttribute("text-anchor", "middle");
        textEl.setAttribute("dominant-baseline", "central");
        textEl.setAttribute("fill", CHART_CONFIG.LABEL_FILL);
        textEl.setAttribute("font-family", CHART_CONFIG.LABEL_FONT_FAMILY);
        textEl.setAttribute("font-size", CHART_CONFIG.LABEL_FONT_SIZE);
        textEl.setAttribute("font-weight", CHART_CONFIG.LABEL_FONT_WEIGHT);
        if (textEl.style) {
          textEl.style.pointerEvents = "none";
          textEl.style.userSelect = "none";
          textEl.style.textShadow = CHART_CONFIG.LABEL_TEXT_SHADOW;
        }
        group.appendChild(pathEl);
        group.appendChild(textEl);
        slicesEl.appendChild(group);
      } else {
        pathEl = _findDirectChildByRole(group, "slice-path");
        textEl = _findDirectChildByRole(group, "slice-label");
      }

      group.setAttribute("opacity", opacity);

      if (pathEl) {
        pathEl.setAttribute("d", pathD);
        pathEl.setAttribute("fill", `url(#${prefix}-grad-${key})`);
        if (pathEl.style) {
          pathEl.style.filter = isActive ? CHART_CONFIG.ACTIVE_DROP_SHADOW : "";
        }
      }

      if (textEl) {
        if (seg.showLabel) {
          textEl.setAttribute("display", "");
          textEl.setAttribute("x", String(isActive ? seg.activeLabelX : seg.labelX));
          textEl.setAttribute("y", String(isActive ? seg.activeLabelY : seg.labelY));
          textEl.textContent = String(seg.percentLabel);
        } else {
          textEl.setAttribute("display", "none");
          textEl.textContent = "";
        }
      }

      // Only reorder if index order actually changed (avoids detach/re-attach flicker)
      const currentAtIdx = (slicesEl.children || slicesEl.childNodes || [])[idx];
      if (currentAtIdx && currentAtIdx !== group) {
        slicesEl.insertBefore(group, currentAtIdx);
      }
    }

    for (const [oldKey, groupNode] of existingGroups.entries()) {
      if (!activeKeys.has(oldKey) && groupNode.parentNode === slicesEl) {
        slicesEl.removeChild(groupNode);
      }
    }

    return true;
  }

  global.MKVForgeCharts = Object.freeze({
    CHART_CONFIG,
    polarToCartesian,
    buildAnnularSectorPath,
    buildPieSlices,
    renderPieSvg,
    patchPieSvg,
  });
})(window);
