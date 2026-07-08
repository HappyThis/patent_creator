import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright';

const FIGURE_ROLES = new Set([
  'node',
  'group',
  'connector',
  'label',
  'port',
  'data',
  'decision',
  'storage',
  'callout',
]);
const NODE_LIKE_ROLES = new Set(['node', 'data', 'decision', 'storage', 'port', 'callout']);
const MIN_CANVAS_MARGIN = 12;
const MIN_TEXT_CLEARANCE = 6;
const MIN_GROUP_PADDING = 12;
const MAX_ISSUES = 120;
const SEMANTIC_NOTE_KIND_PATTERN = /(?:note|callout|annotation|legend|caption|constraint|remark|example|placeholder|explain|description)/i;

function parseArgs(argv) {
  const args = {};
  for (let i = 2; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--')) {
      continue;
    }
    args[key.slice(2)] = argv[i + 1];
    i += 1;
  }
  return args;
}

function round(value) {
  return Number.parseFloat(Number(value || 0).toFixed(2));
}

function overlaps(a, b, padding = 0) {
  return !(
    a.right + padding <= b.x ||
    b.right + padding <= a.x ||
    a.bottom + padding <= b.y ||
    b.bottom + padding <= a.y
  );
}

function containsPoint(rect, point) {
  return point.x >= rect.x && point.x <= rect.right && point.y >= rect.y && point.y <= rect.bottom;
}

function rectCenter(rect) {
  return {
    x: rect.x + rect.width / 2,
    y: rect.y + rect.height / 2,
  };
}

function pointRectDistance(point, rect) {
  const dx = Math.max(rect.x - point.x, 0, point.x - rect.right);
  const dy = Math.max(rect.y - point.y, 0, point.y - rect.bottom);
  return Math.sqrt(dx * dx + dy * dy);
}

function minPointRectDistance(points, rect) {
  if (!points.length) {
    return Number.POSITIVE_INFINITY;
  }
  return Math.min(...points.map((point) => pointRectDistance(point, rect)));
}

function rectInsideCanvas(rect, canvas, margin = 0) {
  return (
    rect.x >= margin &&
    rect.y >= margin &&
    rect.right <= canvas.width - margin &&
    rect.bottom <= canvas.height - margin
  );
}

function elementDisplayName(element) {
  const text = element.label || element.text || '';
  const label = text ? `「${text.slice(0, 24)}」` : '';
  return [element.role, element.kind, element.id, label].filter(Boolean).join('/');
}

function addIssue(issues, issue) {
  if (issues.length >= MAX_ISSUES) {
    return;
  }
  issues.push(issue);
}

function countBends(points) {
  if (points.length < 3) {
    return 0;
  }
  const simplified = [points[0]];
  for (const point of points.slice(1)) {
    const previous = simplified[simplified.length - 1];
    if (Math.hypot(point.x - previous.x, point.y - previous.y) >= 14) {
      simplified.push(point);
    }
  }
  let bends = 0;
  for (let i = 1; i < simplified.length - 1; i += 1) {
    const a = simplified[i - 1];
    const b = simplified[i];
    const c = simplified[i + 1];
    const v1 = { x: b.x - a.x, y: b.y - a.y };
    const v2 = { x: c.x - b.x, y: c.y - b.y };
    const l1 = Math.hypot(v1.x, v1.y);
    const l2 = Math.hypot(v2.x, v2.y);
    if (l1 < 10 || l2 < 10) {
      continue;
    }
    const cos = Math.max(-1, Math.min(1, (v1.x * v2.x + v1.y * v2.y) / (l1 * l2)));
    const angle = Math.acos(cos) * 180 / Math.PI;
    if (angle > 35) {
      bends += 1;
    }
  }
  return bends;
}

function connectorLength(points) {
  let length = 0;
  for (let i = 1; i < points.length; i += 1) {
    length += Math.hypot(points[i].x - points[i - 1].x, points[i].y - points[i - 1].y);
  }
  return length;
}

function rectArea(rect) {
  return Math.max(0, rect.width) * Math.max(0, rect.height);
}

function textCenter(text) {
  return rectCenter(text.bbox);
}

function containedTexts(model, container) {
  return model.texts
    .filter((text) => {
      if (text.figFor) {
        return false;
      }
      return containsPoint(container.bbox, textCenter(text));
    })
    .sort((a, b) => {
      const fontDelta = (b.style.fontSize || 0) - (a.style.fontSize || 0);
      if (Math.abs(fontDelta) > 2) {
        return fontDelta;
      }
      return a.bbox.y - b.bbox.y || a.bbox.x - b.bbox.x;
    });
}

function nodeLabel(model, node) {
  if (node.label) {
    return node.label;
  }
  const directText = String(node.text || '').trim().replace(/\s+/g, ' ');
  if (directText) {
    node.label = directText;
    return node.label;
  }
  const texts = containedTexts(model, node);
  node.label = texts.length ? texts[0].text : '';
  return node.label;
}

function nearestContainingGroup(model, node) {
  const center = rectCenter(node.bbox);
  let best = null;
  for (const group of model.groups) {
    if (group.id === node.id || !containsPoint(group.bbox, center)) {
      continue;
    }
    if (!best || rectArea(group.bbox) < rectArea(best.bbox)) {
      best = group;
    }
  }
  return best;
}

function isSemanticNoteLike(node, label = '') {
  const haystack = [node.role, node.kind, node.id, label].filter(Boolean).join(' ');
  return node.role === 'callout' || SEMANTIC_NOTE_KIND_PATTERN.test(haystack);
}

function normalizeSiblingText(value) {
  let text = String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
  if (!text) {
    return '';
  }
  text = text.replace(/[：:(（].*$/u, '').trim();
  text = text.replace(/[\s_-]*(?:[a-z]|[0-9]+|[一二三四五六七八九十]+)$/iu, '').trim();
  text = text.replace(/[\s_-]+$/u, '').trim();
  return text.length >= 3 ? text : '';
}

function siblingKey(model, node) {
  const label = nodeLabel(model, node);
  const normalizedLabel = normalizeSiblingText(label);
  const normalizedId = normalizeSiblingText(node.id);
  const base = normalizedLabel || normalizedId;
  if (!base) {
    return '';
  }
  return [node.role, node.kind || '', base].join(':');
}

function buildSemanticGraph(model) {
  const nodeIds = new Set(model.nodes.map((node) => node.id).filter(Boolean));
  const degreeByNode = new Map(
    model.nodes.map((node) => [
      node.id,
      {
        in: 0,
        out: 0,
        total: 0,
        kinds: [],
      },
    ]),
  );
  const relationships = [];

  for (const connector of model.connectors) {
    const source = connector.source || '';
    const target = connector.target || '';
    if (!source || !target || source === target) {
      continue;
    }
    relationships.push({
      id: connector.id,
      kind: connector.kind || '',
      source,
      target,
      directed: Boolean(connector.markerEnd || connector.markerStart),
    });
    if (nodeIds.has(source)) {
      const degree = degreeByNode.get(source);
      degree.out += 1;
      degree.total += 1;
      if (connector.kind && !degree.kinds.includes(connector.kind)) {
        degree.kinds.push(connector.kind);
      }
    }
    if (nodeIds.has(target)) {
      const degree = degreeByNode.get(target);
      degree.in += 1;
      degree.total += 1;
      if (connector.kind && !degree.kinds.includes(connector.kind)) {
        degree.kinds.push(connector.kind);
      }
    }
  }

  return { degreeByNode, relationships };
}

function evaluateSemanticStructure(model, issues, graph) {
  const groupByNode = new Map();
  for (const node of model.nodes) {
    node.label = nodeLabel(model, node);
    const group = nearestContainingGroup(model, node);
    groupByNode.set(node.id, group ? group.id : '__root__');
  }

  for (const node of model.nodes) {
    if (isSemanticNoteLike(node, node.label)) {
      continue;
    }
    const degree = graph.degreeByNode.get(node.id);
    if (!degree || degree.total > 0) {
      continue;
    }
    addIssue(issues, {
      severity: 'warning',
      rule: 'semantic_orphan_business_node',
      category: 'semantic',
      elementIds: [node.id],
      message: `业务节点 ${elementDisplayName(node)} 没有任何连接线；若它不是旁注，应接入主路径、控制域、数据流或共享资源关系。`,
    });
  }

  const siblingGroups = new Map();
  for (const node of model.nodes) {
    if (isSemanticNoteLike(node, node.label)) {
      continue;
    }
    const key = siblingKey(model, node);
    if (!key) {
      continue;
    }
    const scopedKey = `${groupByNode.get(node.id) || '__root__'}|${key}`;
    const siblings = siblingGroups.get(scopedKey) || [];
    siblings.push(node);
    siblingGroups.set(scopedKey, siblings);
  }

  for (const siblings of siblingGroups.values()) {
    if (siblings.length < 2) {
      continue;
    }
    const connected = siblings.filter((node) => (graph.degreeByNode.get(node.id)?.total || 0) > 0);
    const disconnected = siblings.filter((node) => (graph.degreeByNode.get(node.id)?.total || 0) === 0);
    if (!connected.length || !disconnected.length) {
      continue;
    }
    const groupId = groupByNode.get(siblings[0].id) || '__root__';
    const severity = siblings.length >= 3 && connected.length >= 2 ? 'error' : 'warning';
    addIssue(issues, {
      severity,
      rule: 'semantic_sibling_connectivity_inconsistent',
      category: 'semantic',
      elementIds: siblings.map((node) => node.id),
      measurement: {
        connectedCount: connected.length,
        disconnectedCount: disconnected.length,
      },
      message: `同组同类节点连接关系不一致：${connected.map((node) => node.label || node.id).join('、')} 已参与连接，但 ${disconnected.map((node) => node.label || node.id).join('、')} 没有任何连接。若这些节点代表同类业务对象，应表达共同接入、受控访问或明确标记为旁注。${groupId === '__root__' ? '' : ` 所在分组：${groupId}。`}`,
    });
  }
}

async function extractGeometry(page) {
  return page.evaluate(({ supportedRoles, nodeLikeRoles }) => {
    const roleSet = new Set(supportedRoles);
    const nodeRoleSet = new Set(nodeLikeRoles);
    const diagram = document.querySelector('#diagram');
    if (!diagram) {
      throw new Error('Missing #diagram root.');
    }

    const rootBox = diagram.getBoundingClientRect();
    const canvas = {
      width: Math.round(rootBox.width),
      height: Math.round(rootBox.height),
    };

    const rectFromDomRect = (rect) => ({
      x: Number((rect.left - rootBox.left).toFixed(2)),
      y: Number((rect.top - rootBox.top).toFixed(2)),
      width: Number(rect.width.toFixed(2)),
      height: Number(rect.height.toFixed(2)),
      right: Number((rect.right - rootBox.left).toFixed(2)),
      bottom: Number((rect.bottom - rootBox.top).toFixed(2)),
    });

    const isRenderable = (element) => {
      const style = window.getComputedStyle(element);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
      }
      const box = element.getBoundingClientRect();
      return box.width > 0 || box.height > 0 || element instanceof SVGGeometryElement;
    };

    const explicitElements = Array.from(diagram.querySelectorAll('[data-fig-role]')).filter(isRenderable);
    const elements = explicitElements.map((element, index) => {
      const role = (element.getAttribute('data-fig-role') || '').trim();
      const style = window.getComputedStyle(element);
      return {
        id: element.getAttribute('data-fig-id') || element.id || `${role || 'element'}-${index + 1}`,
        explicitId: Boolean(element.getAttribute('data-fig-id') || element.id),
        role,
        roleSupported: roleSet.has(role),
        kind: element.getAttribute('data-fig-kind') || '',
        source: element.getAttribute('data-fig-source') || '',
        target: element.getAttribute('data-fig-target') || '',
        figFor: element.getAttribute('data-fig-for') || '',
        tag: element.tagName.toLowerCase(),
        text: (element.textContent || '').trim().replace(/\s+/g, ' '),
        bbox: rectFromDomRect(element.getBoundingClientRect()),
        style: {
          stroke: style.stroke || '',
          fill: style.fill || '',
          strokeWidth: Number.parseFloat(style.strokeWidth || '0') || 0,
          strokeDasharray: style.strokeDasharray || '',
          fontSize: Number.parseFloat(style.fontSize || '0') || 0,
        },
      };
    });

    const nearestFigElement = (element) => element.closest?.('[data-fig-role]');
    const figIdentity = (element) => {
      const fig = nearestFigElement(element);
      if (!fig) {
        return { role: '', id: '', figFor: '' };
      }
      return {
        role: fig.getAttribute('data-fig-role') || '',
        id: fig.getAttribute('data-fig-id') || fig.id || '',
        figFor: fig.getAttribute('data-fig-for') || '',
      };
    };

    const texts = [];
    for (const element of Array.from(diagram.querySelectorAll('text'))) {
      if (!isRenderable(element)) {
        continue;
      }
      const identity = figIdentity(element);
      const text = (element.textContent || '').trim().replace(/\s+/g, ' ');
      if (!text) {
        continue;
      }
      const style = window.getComputedStyle(element);
      texts.push({
        id: element.getAttribute('data-fig-id') || element.id || identity.id || `text-${texts.length + 1}`,
        role: element.getAttribute('data-fig-role') || identity.role || 'text',
        kind: element.getAttribute('data-fig-kind') || '',
        figFor: element.getAttribute('data-fig-for') || identity.figFor || '',
        ownerId: identity.id,
        text,
        bbox: rectFromDomRect(element.getBoundingClientRect()),
        style: {
          fontSize: Number.parseFloat(style.fontSize || '0') || 0,
        },
      });
    }

    const walker = document.createTreeWalker(
      diagram,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (!node.textContent || !node.textContent.trim()) {
            return NodeFilter.FILTER_REJECT;
          }
          const parent = node.parentElement;
          if (!parent || parent.closest('svg text')) {
            return NodeFilter.FILTER_REJECT;
          }
          const style = window.getComputedStyle(parent);
          if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      },
    );

    while (walker.nextNode()) {
      const node = walker.currentNode;
      const range = document.createRange();
      range.selectNodeContents(node);
      const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0 && rect.height > 0);
      const parent = node.parentElement;
      const identity = figIdentity(parent);
      const style = window.getComputedStyle(parent);
      for (const rect of rects) {
        texts.push({
          id: parent.getAttribute('data-fig-id') || parent.id || identity.id || `text-${texts.length + 1}`,
          role: parent.getAttribute('data-fig-role') || identity.role || 'text',
          kind: parent.getAttribute('data-fig-kind') || '',
          figFor: parent.getAttribute('data-fig-for') || identity.figFor || '',
          ownerId: identity.id,
          text: node.textContent.trim().replace(/\s+/g, ' '),
          bbox: rectFromDomRect(rect),
          style: {
            fontSize: Number.parseFloat(style.fontSize || '0') || 0,
          },
        });
      }
    }

    const toRootPoint = (element, point) => {
      const svg = element.ownerSVGElement;
      const svgPoint = svg.createSVGPoint();
      svgPoint.x = point.x;
      svgPoint.y = point.y;
      const ctm = element.getScreenCTM();
      if (!ctm) {
        return null;
      }
      const screenPoint = svgPoint.matrixTransform(ctm);
      return {
        x: Number((screenPoint.x - rootBox.left).toFixed(2)),
        y: Number((screenPoint.y - rootBox.top).toFixed(2)),
      };
    };

    const sampleConnector = (element) => {
      if (!(element instanceof SVGGeometryElement) || typeof element.getTotalLength !== 'function') {
        return [];
      }
      let length = 0;
      try {
        length = element.getTotalLength();
      } catch {
        return [];
      }
      if (!Number.isFinite(length) || length <= 0) {
        return [];
      }
      const points = [];
      const step = 8;
      for (let offset = 0; offset < length; offset += step) {
        const point = element.getPointAtLength(offset);
        const rootPoint = toRootPoint(element, point);
        if (rootPoint) {
          points.push(rootPoint);
        }
      }
      const endPoint = toRootPoint(element, element.getPointAtLength(length));
      if (endPoint) {
        points.push(endPoint);
      }
      return points;
    };

    const explicitConnectors = explicitElements.filter((element) => (element.getAttribute('data-fig-role') || '') === 'connector');
    const fallbackConnectors = Array.from(diagram.querySelectorAll('svg line, svg polyline, svg path')).filter((element) => {
      if (explicitConnectors.includes(element) || !isRenderable(element)) {
        return false;
      }
      if (element.closest('defs, marker, clipPath, mask, pattern')) {
        return false;
      }
      const style = window.getComputedStyle(element);
      const hasStroke = style.stroke && style.stroke !== 'none' && style.stroke !== 'rgba(0, 0, 0, 0)';
      const fill = style.fill || '';
      const hasMarker = Boolean(element.getAttribute('marker-end') || element.getAttribute('marker-start'));
      return hasStroke && (hasMarker || fill === 'none' || element.tagName.toLowerCase() !== 'path');
    });

    const connectors = [...explicitConnectors, ...fallbackConnectors].map((element, index) => {
      const style = window.getComputedStyle(element);
      const points = sampleConnector(element);
      const role = element.getAttribute('data-fig-role') || 'connector';
      return {
        id: element.getAttribute('data-fig-id') || element.id || `connector-${index + 1}`,
        explicitId: Boolean(element.getAttribute('data-fig-id') || element.id),
        role,
        inferred: role !== 'connector' || !element.hasAttribute('data-fig-role'),
        kind: element.getAttribute('data-fig-kind') || '',
        source: element.getAttribute('data-fig-source') || '',
        target: element.getAttribute('data-fig-target') || '',
        figFor: element.getAttribute('data-fig-for') || '',
        tag: element.tagName.toLowerCase(),
        bbox: rectFromDomRect(element.getBoundingClientRect()),
        points,
        markerStart: element.getAttribute('marker-start') || style.markerStart || '',
        markerEnd: element.getAttribute('marker-end') || style.markerEnd || '',
        dashed: Boolean(style.strokeDasharray && style.strokeDasharray !== 'none' && !/^0(px)?$/i.test(style.strokeDasharray)),
        style: {
          stroke: style.stroke || '',
          strokeWidth: Number.parseFloat(style.strokeWidth || '0') || 0,
          strokeDasharray: style.strokeDasharray || '',
        },
      };
    });

    return {
      version: 1,
      canvas,
      elements,
      texts,
      nodes: elements.filter((element) => nodeRoleSet.has(element.role)),
      groups: elements.filter((element) => element.role === 'group'),
      labels: elements.filter((element) => element.role === 'label'),
      connectors,
    };
  }, { supportedRoles: [...FIGURE_ROLES], nodeLikeRoles: [...NODE_LIKE_ROLES] });
}

function evaluateGeometry(model) {
  const issues = [];
  const elementById = new Map();
  for (const element of [...model.elements, ...model.connectors]) {
    if (element.id && !elementById.has(element.id)) {
      elementById.set(element.id, element);
    }
  }
  const graph = buildSemanticGraph(model);

  if (model.elements.length === 0) {
    addIssue(issues, {
      severity: 'warning',
      rule: 'missing_fig_roles',
      message: 'diagram.html 中没有 data-fig-role 图元，几何检查只能做有限的启发式检查。',
    });
  }

  for (const element of model.elements) {
    if (!element.roleSupported) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'unsupported_fig_role',
        elementIds: [element.id],
        message: `图元 ${elementDisplayName(element)} 使用了未支持的 data-fig-role。`,
      });
    }
    if (!element.explicitId && ['node', 'group', 'connector', 'label', 'data', 'decision', 'storage'].includes(element.role)) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'missing_fig_id',
        elementIds: [element.id],
        message: `主要图元 ${elementDisplayName(element)} 缺少 data-fig-id 或 id，后续修图定位不稳定。`,
      });
    }
  }

  for (const text of model.texts) {
    if (!rectInsideCanvas(text.bbox, model.canvas, MIN_CANVAS_MARGIN)) {
      addIssue(issues, {
        severity: 'error',
        rule: 'text_out_of_bounds',
        elementIds: [text.id],
        measurement: { marginPx: MIN_CANVAS_MARGIN },
        message: `文本「${text.text.slice(0, 32)}」贴边或超出画布安全边距。`,
      });
    }
    if (text.style.fontSize > 0 && text.style.fontSize < 11) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'text_too_small',
        elementIds: [text.id],
        measurement: { fontSizePx: round(text.style.fontSize), minFontSizePx: 11 },
        message: `文本「${text.text.slice(0, 32)}」字号过小，可能影响可读性。`,
      });
    }
  }

  for (let i = 0; i < model.texts.length; i += 1) {
    for (let j = i + 1; j < model.texts.length; j += 1) {
      const a = model.texts[i];
      const b = model.texts[j];
      if (a.id === b.id && a.text === b.text) {
        continue;
      }
      if (overlaps(a.bbox, b.bbox, 1)) {
        addIssue(issues, {
          severity: 'error',
          rule: 'text_overlap',
          elementIds: [a.id, b.id],
          message: `文本「${a.text.slice(0, 24)}」与「${b.text.slice(0, 24)}」发生重叠。`,
        });
      }
    }
  }

  for (let i = 0; i < model.nodes.length; i += 1) {
    for (let j = i + 1; j < model.nodes.length; j += 1) {
      const a = model.nodes[i];
      const b = model.nodes[j];
      if (overlaps(a.bbox, b.bbox, 2)) {
        addIssue(issues, {
          severity: 'error',
          rule: 'node_overlap',
          elementIds: [a.id, b.id],
          message: `节点 ${elementDisplayName(a)} 与 ${elementDisplayName(b)} 发生重叠。`,
        });
      }
    }
  }

  for (const connector of model.connectors) {
    if (!connector.points.length) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'connector_not_measurable',
        elementIds: [connector.id],
        message: `连线 ${connector.id} 无法采样，可能使用了检查器暂不支持的 HTML/CSS 画线方式。`,
      });
      continue;
    }

    connector.length = round(connectorLength(connector.points));
    connector.bends = countBends(connector.points);
    const longConnectorThreshold = model.canvas.width * 0.45;
    if (connector.length > longConnectorThreshold) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'connector_too_long',
        elementIds: [connector.id],
        measurement: { lengthPx: connector.length, maxRecommendedPx: round(longConnectorThreshold) },
        message: `连线 ${connector.id} 过长，建议拆成局部关系、接口节点或更短折线。`,
      });
    }
    if (connector.bends > 4) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'too_many_bends',
        elementIds: [connector.id],
        measurement: { bendCount: connector.bends, maxRecommended: 4 },
        message: `连线 ${connector.id} 折点过多，阅读路径可能不清晰。`,
      });
    }
    if (connector.kind === 'main-flow' && connector.dashed) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'main_flow_should_not_be_dashed',
        elementIds: [connector.id],
        message: `主流程连线 ${connector.id} 使用了虚线，容易削弱主阅读路径。`,
      });
    }
    if (connector.dashed && connector.length > 80) {
      const hasLabel = model.texts.some((text) => text.figFor === connector.id) || model.labels.some((label) => label.figFor === connector.id);
      if (!hasLabel) {
        addIssue(issues, {
          severity: 'warning',
          rule: 'dashed_connector_without_label',
          elementIds: [connector.id],
          message: `虚线 ${connector.id} 缺少线旁标签，弱依赖/反馈/可选等语义不够明确。`,
        });
      }
    }

    if (!connector.source || !connector.target) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'connector_missing_endpoint_metadata',
        elementIds: [connector.id],
        message: `连线 ${connector.id} 缺少 data-fig-source 或 data-fig-target，无法稳定检查起止关系。`,
      });
    }

    for (const [side, endpointId, point] of [
      ['source', connector.source, connector.points[0]],
      ['target', connector.target, connector.points[connector.points.length - 1]],
    ]) {
      if (!endpointId) {
        continue;
      }
      const endpoint = elementById.get(endpointId);
      if (!endpoint) {
        addIssue(issues, {
          severity: 'error',
          rule: 'connector_endpoint_missing',
          elementIds: [connector.id],
          message: `连线 ${connector.id} 的 ${side}=${endpointId} 找不到对应图元。`,
        });
        continue;
      }
      const distance = pointRectDistance(point, endpoint.bbox);
      if (distance > 16) {
        addIssue(issues, {
          severity: 'error',
          rule: 'connector_endpoint_detached',
          elementIds: [connector.id, endpoint.id],
          measurement: { distancePx: round(distance), maxDistancePx: 16 },
          message: `连线 ${connector.id} 的 ${side} 端点没有贴近图元 ${endpoint.id}。`,
        });
      }
    }

    for (const text of model.texts) {
      const crosses = connector.points.some((point) => containsPoint(text.bbox, point));
      if (crosses) {
        addIssue(issues, {
          severity: 'error',
          rule: 'connector_crosses_text',
          elementIds: [connector.id, text.id],
          message: `连线 ${connector.id} 穿过文本「${text.text.slice(0, 32)}」。`,
        });
        continue;
      }
      if (text.figFor !== connector.id) {
        const distance = minPointRectDistance(connector.points, text.bbox);
        if (distance < MIN_TEXT_CLEARANCE) {
          addIssue(issues, {
            severity: 'warning',
            rule: 'connector_too_close_to_text',
            elementIds: [connector.id, text.id],
            measurement: { clearancePx: round(distance), minClearancePx: MIN_TEXT_CLEARANCE },
            message: `连线 ${connector.id} 距离文本「${text.text.slice(0, 32)}」过近。`,
          });
        }
      }
    }

    for (const node of model.nodes) {
      if (node.id === connector.source || node.id === connector.target) {
        continue;
      }
      const crosses = connector.points.some((point) => containsPoint(node.bbox, point));
      if (crosses) {
        addIssue(issues, {
          severity: 'error',
          rule: 'connector_crosses_node',
          elementIds: [connector.id, node.id],
          message: `连线 ${connector.id} 穿过非目标节点 ${elementDisplayName(node)}。`,
        });
      }
    }
  }

  for (const group of model.groups) {
    if (group.bbox.width > model.canvas.width * 0.92 && group.bbox.height > model.canvas.height * 0.85) {
      addIssue(issues, {
        severity: 'warning',
        rule: 'oversized_group',
        elementIds: [group.id],
        message: `分组 ${elementDisplayName(group)} 接近覆盖整张图，可能变成装饰性大外框。`,
      });
    }
    for (const node of model.nodes) {
      const center = rectCenter(node.bbox);
      if (!containsPoint(group.bbox, center)) {
        continue;
      }
      const padding = Math.min(
        node.bbox.x - group.bbox.x,
        node.bbox.y - group.bbox.y,
        group.bbox.right - node.bbox.right,
        group.bbox.bottom - node.bbox.bottom,
      );
      if (padding < MIN_GROUP_PADDING) {
        addIssue(issues, {
          severity: 'warning',
          rule: 'group_padding_too_small',
          elementIds: [group.id, node.id],
          measurement: { paddingPx: round(padding), minPaddingPx: MIN_GROUP_PADDING },
          message: `分组 ${group.id} 与内部节点 ${node.id} 距离过近。`,
        });
      }
    }
  }

  evaluateSemanticStructure(model, issues, graph);

  const errors = issues.filter((issue) => issue.severity === 'error').length;
  const connectedNodeCount = [...graph.degreeByNode.values()].filter((degree) => degree.total > 0).length;
  return {
    version: 1,
    ok: errors === 0,
    metrics: {
      elementCount: model.elements.length,
      textCount: model.texts.length,
      nodeCount: model.nodes.length,
      groupCount: model.groups.length,
      connectorCount: model.connectors.length,
      relationshipCount: graph.relationships.length,
      connectedNodeCount,
      orphanNodeCount: Math.max(0, model.nodes.length - connectedNodeCount),
    },
    issues,
  };
}

async function main() {
  const args = parseArgs(process.argv);
  if (!args.input || !args.output) {
    throw new Error('Usage: node render-figure-html.mjs --input diagram.html --output render.png --width 1500 --height 900 [--geometry-output geometry.json] [--geometry-report-output geometry_report.json]');
  }

  const inputPath = path.resolve(args.input);
  const outputPath = path.resolve(args.output);
  const geometryOutputPath = args['geometry-output'] ? path.resolve(args['geometry-output']) : null;
  const geometryReportOutputPath = args['geometry-report-output'] ? path.resolve(args['geometry-report-output']) : null;
  const width = Number.parseInt(args.width || '1500', 10);
  const height = Number.parseInt(args.height || '900', 10);
  if (!Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0) {
    throw new Error('Invalid figure viewport size.');
  }

  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  const browser = await chromium.launch({ headless: true });
  try {
    const inputUrl = pathToFileURL(inputPath).href;
    const page = await browser.newPage({
      viewport: { width, height },
      deviceScaleFactor: 1,
    });
    await page.route('**/*', (route) => {
      const requestUrl = route.request().url();
      if (requestUrl === inputUrl || requestUrl.startsWith('data:') || requestUrl === 'about:blank') {
        route.continue();
        return;
      }
      route.abort();
    });
    await page.goto(inputUrl, { waitUntil: 'load' });
    await page.evaluate(async ({ viewportWidth, viewportHeight }) => {
      document.documentElement.style.width = `${viewportWidth}px`;
      document.documentElement.style.height = `${viewportHeight}px`;
      document.body.style.width = `${viewportWidth}px`;
      document.body.style.height = `${viewportHeight}px`;
      document.body.style.margin = '0';
      document.body.style.overflow = 'hidden';
      await document.fonts?.ready;
    }, { viewportWidth: width, viewportHeight: height });
    const geometry = await extractGeometry(page);
    const geometryReport = evaluateGeometry(geometry);
    if (geometryOutputPath) {
      await fs.mkdir(path.dirname(geometryOutputPath), { recursive: true });
      await fs.writeFile(geometryOutputPath, `${JSON.stringify(geometry, null, 2)}\n`, 'utf-8');
    }
    if (geometryReportOutputPath) {
      await fs.mkdir(path.dirname(geometryReportOutputPath), { recursive: true });
      await fs.writeFile(geometryReportOutputPath, `${JSON.stringify(geometryReport, null, 2)}\n`, 'utf-8');
    }
    await page.screenshot({
      path: outputPath,
      clip: { x: 0, y: 0, width, height },
      omitBackground: false,
    });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
