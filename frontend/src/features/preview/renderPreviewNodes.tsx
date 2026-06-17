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
  parentIndexPath?: number[];
};

export function renderPreviewNodes({
  nodes,
  recentSectionIds,
  recentBlockIds,
  sectionStatusById,
  figuresById,
  parentIndexPath = [],
}: RenderPreviewNodesProps): JSX.Element[] {
  let sectionIndex = 0;
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
          {renderFigureRefs(node.text, figuresById)}
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
            <li key={`${node.id}_${index}`}>{renderFigureRefs(item, figuresById)}</li>
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
                    <td key={`${node.id}_${rowIndex}_${index}`}>{renderFigureRefs(cell, figuresById)}</td>
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
          className={`preview-block preview-formula ${blockChanged ? 'changed' : ''} ${renderFailed ? 'preview-formula-fallback' : ''}`}
          data-block-id={node.id}
        >
          {renderFailed ? (
            <code>{node.latex}</code>
          ) : (
            <div className="preview-formula-content" dangerouslySetInnerHTML={{ __html: rendered }} />
          )}
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

function renderFigureRefs(text: string, figuresById: Record<string, FigureRenderAsset>) {
  const pattern = /\[([^\]]+)\]\(figure:(fig_\d{6})\)/g;
  const parts: JSX.Element[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(<span key={`text_${lastIndex}`}>{text.slice(lastIndex, match.index)}</span>);
    }
    const figureId = match[2];
    const figure = figuresById[figureId];
    parts.push(
      <a key={`figure_${match.index}`} className="preview-figure-ref" href={`#figure-${figureId}`}>
        {figure?.label ?? match[1]}
      </a>,
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    parts.push(<span key={`text_${lastIndex}`}>{text.slice(lastIndex)}</span>);
  }
  return parts.length ? parts : text;
}
