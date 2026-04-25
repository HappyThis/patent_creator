import { OutlineItem } from '../../types';
import { OutlineNode } from './OutlineNode';

type OutlinePanelProps = {
  outline: OutlineItem[];
  activeSectionId: string;
  recentSectionIds: string[];
  onSelect: (sectionId: string) => void;
};

function flattenOutline(items: OutlineItem[]): OutlineItem[] {
  return items.flatMap((item) => [item, ...(item.children ? flattenOutline(item.children) : [])]);
}

export function OutlinePanel({
  outline,
  activeSectionId,
  recentSectionIds,
  onSelect,
}: OutlinePanelProps) {
  const outlineIndex = flattenOutline(outline);

  return (
    <aside className="panel outline-panel">
      <div className="panel-header">
        <h2>目录</h2>
        <span>{outlineIndex.length} sections</span>
      </div>
      <div className="outline-list">
        {outline.map((item) => (
          <OutlineNode
            key={item.id}
            item={item}
            activeSectionId={activeSectionId}
            recentSectionIds={recentSectionIds}
            onSelect={onSelect}
          />
        ))}
      </div>
    </aside>
  );
}
