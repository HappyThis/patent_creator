import { OutlineItem } from '../../types';
import { SectionStatus } from '../preview/documentStats';

type OutlineNodeProps = {
  item: OutlineItem;
  indexPath: number[];
  activeSectionId: string;
  recentSectionIds: string[];
  sectionStatusById: Record<string, SectionStatus>;
  onSelect: (sectionId: string) => void;
};

export function OutlineNode({
  item,
  indexPath,
  activeSectionId,
  recentSectionIds,
  sectionStatusById,
  onSelect,
}: OutlineNodeProps) {
  const isActive = activeSectionId === item.id;
  const isRecent = recentSectionIds.includes(item.id);
  const sectionStatus = sectionStatusById[item.id];
  const sectionNumber = indexPath.join('.');
  const statusLabel = sectionStatus?.filled ? '已填写' : '待补充';

  return (
    <div className="outline-node">
      <button
        className={`outline-button level-${item.level} ${isActive ? 'active' : ''} ${isRecent ? 'recent' : ''}`}
        onClick={() => onSelect(item.id)}
        title={`${sectionNumber}. ${item.title}`}
      >
        <span className="outline-copy">
          <span className="outline-number">{sectionNumber}.</span>
          <span className="outline-label">{item.title}</span>
        </span>
        <span className={`outline-status ${sectionStatus?.filled ? 'filled' : 'empty'}`} aria-label={statusLabel} title={statusLabel} />
        <span className="outline-action" aria-hidden="true">›</span>
      </button>
      {item.children?.length ? (
        <div className="outline-children">
          {item.children.map((child, index) => (
            <OutlineNode
              key={child.id}
              item={child}
              indexPath={[...indexPath, index + 1]}
              activeSectionId={activeSectionId}
              recentSectionIds={recentSectionIds}
              sectionStatusById={sectionStatusById}
              onSelect={onSelect}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
