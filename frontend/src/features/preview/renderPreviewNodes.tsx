import { RenderNode } from '../../types';
import { isSectionDirectlyEmpty, SectionStatus } from './documentStats';

type RenderPreviewNodesProps = {
  nodes: RenderNode[];
  recentSectionIds: string[];
  recentBlockIds: string[];
  sectionStatusById: Record<string, SectionStatus>;
  parentIndexPath?: number[];
};

export function renderPreviewNodes({
  nodes,
  recentSectionIds,
  recentBlockIds,
  sectionStatusById,
  parentIndexPath = [],
}: RenderPreviewNodesProps): JSX.Element[] {
  let sectionIndex = 0;
  return nodes.map((node) => {
    if (node.type === 'section') {
      sectionIndex += 1;
      const indexPath = [...parentIndexPath, sectionIndex];
      const sectionChanged = recentSectionIds.includes(node.id);
      const isEmpty = isSectionDirectlyEmpty(node);
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
          {isEmpty ? (
            <p className="preview-empty-hint">
              等待补充{node.title}内容
            </p>
          ) : null}
          <div className="preview-children">
            {renderPreviewNodes({
              nodes: node.children,
              recentSectionIds,
              recentBlockIds,
              sectionStatusById,
              parentIndexPath: indexPath,
            })}
          </div>
        </section>
      );
    }

    const blockChanged = recentBlockIds.includes(node.id);
    if (node.type === 'paragraph') {
      return (
        <p
          key={node.id}
          className={`preview-block ${blockChanged ? 'changed' : ''}`}
          data-block-id={node.id}
        >
          {node.text}
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
          {node.items.map((item) => (
            <li key={item}>{item}</li>
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
                    <td key={`${node.id}_${rowIndex}_${index}`}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }

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
  });
}
