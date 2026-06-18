import type { ReactNode } from 'react';
import { FigureRenderAsset, RenderNode } from '../../types';
import katex from 'katex';
import { isSectionDirectlyEmpty, SectionStatus } from './documentStats';
import { PreviewFigure } from './PreviewFigure';

type RenderPreviewNodesProps = {
  nodes: RenderNode[];
  recentSectionIds: string[];
  recentBlockIds: string[];
  sectionStatusById: Record<string, SectionStatus>;
  figuresById: Record<string, FigureRenderAsset>;
  formulaNumberById?: Record<string, number>;
  parentIndexPath?: number[];
};

export function renderPreviewNodes({
  nodes,
  recentSectionIds,
  recentBlockIds,
  sectionStatusById,
  figuresById,
  formulaNumberById,
  parentIndexPath = [],
}: RenderPreviewNodesProps): JSX.Element[] {
  let sectionIndex = 0;
  const resolvedFormulaNumberById = formulaNumberById ?? collectFormulaNumbers(nodes);
  return nodes.map((node) => {
    if (node.type === 'section') {
      sectionIndex += 1;
      const indexPath = [...parentIndexPath, sectionIndex];
      const sectionChanged = recentSectionIds.includes(node.id);
      const hasChildSections = node.children.some((child) => child.type === 'section');
      const hasSubtreeContent = sectionStatusById[node.id]?.filled ?? !isSectionDirectlyEmpty(node);
      const isEmpty = !hasChildSections && !hasSubtreeContent;
      const heading = (
        <>
          <span className="preview-heading-index">{indexPath.join('.')}.</span>
          <span>{node.title}</span>
        </>
      );
      return (
        <section
          key={node.id}
          className={`preview-section level-${node.level} ${isEmpty ? 'empty' : 'filled'} ${sectionChanged ? 'changed' : ''}`}
          data-anchor={node.anchor}
        >
          {node.level === 2 ? <h2>{heading}</h2> : <h3>{heading}</h3>}
          {isEmpty ? <p className="preview-empty-hint">内容待补充</p> : null}
          <div className="preview-children">
            {renderPreviewNodes({
              nodes: node.children,
              recentSectionIds,
              recentBlockIds,
              sectionStatusById,
              figuresById,
              formulaNumberById: resolvedFormulaNumberById,
              parentIndexPath: indexPath,
            })}
          </div>
        </section>
      );
    }

    const blockChanged = recentBlockIds.includes(node.id);
    if (node.type === 'title' || node.type === 'paragraph') {
      return (
        <p
          key={node.id}
          className={[
            'preview-block',
            node.type === 'paragraph' ? 'preview-paragraph' : 'preview-title-block',
            blockChanged ? 'changed' : '',
          ].filter(Boolean).join(' ')}
          data-block-id={node.id}
        >
          {renderInlineContent(node.text, figuresById, resolvedFormulaNumberById)}
        </p>
      );
    }

    if (node.type === 'list') {
      const Tag = node.ordered ? 'ol' : 'ul';
      return (
        <Tag
          key={node.id}
          className={`preview-block preview-list ${blockChanged ? 'changed' : ''}`}
          data-block-id={node.id}
        >
          {node.items.map((item, index) => (
            <li key={`${node.id}_${index}`}>{renderInlineContent(item, figuresById, resolvedFormulaNumberById)}</li>
          ))}
        </Tag>
      );
    }

    if (node.type === 'table') {
      return (
        <div
          key={node.id}
          className={`preview-block preview-table ${blockChanged ? 'changed' : ''}`}
          data-block-id={node.id}
        >
          <table>
            <thead>
              <tr>
                {node.columns.map((column) => (
                  <th key={column}>{column}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {node.rows.map((row, rowIndex) => (
                <tr key={`${node.id}_${rowIndex}`}>
                  {row.map((cell, index) => (
                    <td key={`${node.id}_${rowIndex}_${index}`}>
                      {renderInlineContent(cell, figuresById, resolvedFormulaNumberById)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

    if (node.type === 'image') {
      return (
        <figure
          key={node.id}
          className={`preview-block preview-image ${blockChanged ? 'changed' : ''}`}
          data-block-id={node.id}
        >
          <div className="image-placeholder">{node.alt ?? '示意图'}</div>
          {node.caption ? <figcaption>{node.caption}</figcaption> : null}
        </figure>
      );
    }

    if (node.type === 'formula') {
      const formulaNumber = resolvedFormulaNumberById[node.id];
      let rendered = '';
      let renderFailed = false;
      try {
        rendered = katex.renderToString(node.latex, {
          displayMode: true,
          throwOnError: true,
          strict: false,
        });
      } catch {
        renderFailed = true;
      }
      return (
        <div
          key={node.id}
          id={`formula-${node.id}`}
          className={`preview-block preview-formula ${blockChanged ? 'changed' : ''} ${renderFailed ? 'preview-formula-fallback' : ''}`}
          data-block-id={node.id}
          data-formula-number={formulaNumber ?? undefined}
        >
          {renderFailed ? (
            <code>{node.latex}</code>
          ) : (
            <div className="preview-formula-content" dangerouslySetInnerHTML={{ __html: rendered }} />
          )}
          {formulaNumber ? (
            <span className="preview-formula-number" aria-label={`式 ${formulaNumber}`}>
              ({formulaNumber})
            </span>
          ) : null}
        </div>
      );
    }

    if (node.type === 'figure') {
      const figure = figuresById[node.figure_id];
      const caption = figure ? `${figure.label} ${figure.title}`.trim() : `图? ${node.figure_id}`;
      return (
        <figure
          key={node.id}
          id={`figure-${node.figure_id}`}
          className={`preview-block preview-figure ${blockChanged ? 'changed' : ''} ${figure ? '' : 'missing'}`}
          data-block-id={node.id}
          data-figure-id={node.figure_id}
        >
          <PreviewFigure figure={figure} figureId={node.figure_id} />
          <figcaption>{caption}</figcaption>
        </figure>
      );
    }

    const exhaustive: never = node;
    return exhaustive;
  });
}

function collectFormulaNumbers(nodes: RenderNode[]): Record<string, number> {
  const formulaNumberById: Record<string, number> = {};
  let nextNumber = 1;
  const visit = (items: RenderNode[]) => {
    for (const item of items) {
      if (item.type === 'section') {
        visit(item.children);
      } else if (item.type === 'formula') {
        formulaNumberById[item.id] = nextNumber;
        nextNumber += 1;
      }
    }
  };
  visit(nodes);
  return formulaNumberById;
}

function renderInlineContent(
  text: string,
  figuresById: Record<string, FigureRenderAsset>,
  formulaNumberById: Record<string, number>,
): ReactNode {
  const parts: ReactNode[] = [];
  let cursor = 0;
  let key = 0;

  while (cursor < text.length) {
    const nextFigure = findNextFigureRef(text, cursor);
    const nextFormula = findNextFormulaRef(text, cursor);
    const nextMath = findNextInlineMath(text, cursor);
    const nextToken = pickEarlierToken(nextFigure, nextFormula, nextMath);

    if (!nextToken) {
      break;
    }
    if (nextToken.start > cursor) {
      parts.push(<span key={`text_${key++}`}>{unescapeInlineText(text.slice(cursor, nextToken.start))}</span>);
    }
    if (nextToken.type === 'figure') {
      const figure = figuresById[nextToken.figureId];
      parts.push(
        <a key={`figure_${key++}`} className="preview-figure-ref" href={`#figure-${nextToken.figureId}`}>
          {figure?.label ?? nextToken.label}
        </a>,
      );
    } else if (nextToken.type === 'formula') {
      const formulaNumber = formulaNumberById[nextToken.blockId];
      parts.push(
        <a key={`formula_${key++}`} className="preview-formula-ref" href={`#formula-${nextToken.blockId}`}>
          {formulaNumber ? `式(${formulaNumber})` : nextToken.label}
        </a>,
      );
    } else {
      parts.push(renderInlineMath(nextToken.latex, nextToken.raw, key++));
    }
    cursor = nextToken.end;
  }

  if (cursor < text.length) {
    parts.push(<span key={`text_${key++}`}>{unescapeInlineText(text.slice(cursor))}</span>);
  }
  return parts.length ? parts : unescapeInlineText(text);
}

type FigureToken = {
  type: 'figure';
  start: number;
  end: number;
  label: string;
  figureId: string;
};

type FormulaRefToken = {
  type: 'formula';
  start: number;
  end: number;
  label: string;
  blockId: string;
};

type MathToken = {
  type: 'math';
  start: number;
  end: number;
  latex: string;
  raw: string;
};

function findNextFigureRef(text: string, offset: number): FigureToken | null {
  const pattern = /\[([^\]]+)\]\(figure:(fig_\d{6})\)/g;
  pattern.lastIndex = offset;
  const match = pattern.exec(text);
  if (!match) {
    return null;
  }
  return {
    type: 'figure',
    start: match.index,
    end: match.index + match[0].length,
    label: match[1],
    figureId: match[2],
  };
}

function findNextFormulaRef(text: string, offset: number): FormulaRefToken | null {
  const pattern = /\[([^\]]+)\]\(formula:([A-Za-z0-9_-]+)\)/g;
  pattern.lastIndex = offset;
  const match = pattern.exec(text);
  if (!match) {
    return null;
  }
  return {
    type: 'formula',
    start: match.index,
    end: match.index + match[0].length,
    label: match[1],
    blockId: match[2],
  };
}

function findNextInlineMath(text: string, offset: number): MathToken | null {
  for (let start = offset; start < text.length; start += 1) {
    if (text[start] !== '$' || isEscaped(text, start) || text[start + 1] === '$') {
      continue;
    }
    for (let end = start + 1; end < text.length; end += 1) {
      if (text[end] === '\n') {
        break;
      }
      if (text[end] === '$' && !isEscaped(text, end)) {
        const latex = text.slice(start + 1, end).trim();
        if (!latex) {
          break;
        }
        return {
          type: 'math',
          start,
          end: end + 1,
          latex,
          raw: text.slice(start, end + 1),
        };
      }
    }
  }
  return null;
}

type InlineToken = FigureToken | FormulaRefToken | MathToken;

function pickEarlierToken(...tokens: Array<InlineToken | null>): InlineToken | null {
  let earlier: InlineToken | null = null;
  for (const token of tokens) {
    if (token && (!earlier || token.start < earlier.start)) {
      earlier = token;
    }
  }
  return earlier;
}

function renderInlineMath(latex: string, raw: string, key: number): ReactNode {
  try {
    const rendered = katex.renderToString(latex, {
      displayMode: false,
      throwOnError: true,
      strict: false,
    });
    return <span key={`math_${key}`} className="preview-inline-math" dangerouslySetInnerHTML={{ __html: rendered }} />;
  } catch {
    return <span key={`math_${key}`}>{raw}</span>;
  }
}

function isEscaped(text: string, index: number): boolean {
  let slashCount = 0;
  for (let cursor = index - 1; cursor >= 0 && text[cursor] === '\\'; cursor -= 1) {
    slashCount += 1;
  }
  return slashCount % 2 === 1;
}

function unescapeInlineText(text: string): string {
  return text.replace(/\\\$/g, '$');
}
