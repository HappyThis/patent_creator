import { OutlineItem } from '../../types';

type OutlineNodeProps = {
  item: OutlineItem;
  activeSectionId: string;
  recentSectionIds: string[];
  onSelect: (sectionId: string) => void;
};

export function OutlineNode({
  item,
  activeSectionId,
  recentSectionIds,
  onSelect,
}: OutlineNodeProps) {
  const isActive = activeSectionId === item.id;
  const isRecent = recentSectionIds.includes(item.id);

  return (
    <div className="outline-node">
      <button
        className={`outline-button level-${item.level} ${isActive ? 'active' : ''} ${isRecent ? 'recent' : ''}`}
        onClick={() => onSelect(item.id)}
      >
        <span>{item.title}</span>
      </button>
      {item.children?.length ? (
        <div className="outline-children">
          {item.children.map((child) => (
            <OutlineNode
              key={child.id}
              item={child}
              activeSectionId={activeSectionId}
              recentSectionIds={recentSectionIds}
              onSelect={onSelect}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
