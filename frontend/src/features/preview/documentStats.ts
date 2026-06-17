import { RenderAst, RenderNode } from '../../types';

export type SectionStatus = {
  characters: number;
  blockCount: number;
  filled: boolean;
};

export type DocumentStats = {
  characters: number;
  totalSections: number;
  filledSections: number;
  sectionStatusById: Record<string, SectionStatus>;
};

export function buildDocumentStats(renderAst: RenderAst): DocumentStats {
  const sectionStatusById: Record<string, SectionStatus> = {};
  let characters = 0;
  let totalSections = 0;
  let filledSections = 0;

  const visit = (nodes: RenderNode[]): { characters: number; blockCount: number } => {
    let subtreeCharacters = 0;
    let subtreeBlockCount = 0;

    for (const node of nodes) {
      if (node.type === 'section') {
        totalSections += 1;
        const sectionStats = visit(node.children);
        const filled = sectionStats.characters > 0;
        sectionStatusById[node.id] = {
          ...sectionStats,
          filled,
        };
        if (filled) {
          filledSections += 1;
        }
        subtreeCharacters += sectionStats.characters;
        subtreeBlockCount += sectionStats.blockCount;
        continue;
      }

      const nodeCharacters = countNodeText(node);
      if (nodeCharacters > 0) {
        subtreeBlockCount += 1;
      }
      subtreeCharacters += nodeCharacters;
    }

    return { characters: subtreeCharacters, blockCount: subtreeBlockCount };
  };

  const totals = visit(renderAst.children);
  characters = totals.characters;

  return { characters, totalSections, filledSections, sectionStatusById };
}

export function isSectionDirectlyEmpty(node: RenderNode): boolean {
  if (node.type !== 'section') {
    return false;
  }

  return node.children.every((child) => child.type === 'section' || countNodeText(child) === 0);
}

function countNodeText(node: RenderNode): number {
  if (node.type === 'section') {
    return 0;
  }
  if (node.type === 'title' || node.type === 'paragraph') {
    return countText(node.text);
  }
  if (node.type === 'list') {
    return countText(node.items.join(''));
  }
  if (node.type === 'table') {
    return countText([...node.columns, ...node.rows.flat()].join(''));
  }
  if (node.type === 'image') {
    return countText(`${node.caption ?? ''}${node.alt ?? ''}`);
  }
  if (node.type === 'formula') {
    return countText(node.latex);
  }
  if (node.type === 'figure') {
    return countText(node.figure_id);
  }
  return 0;
}

function countText(value: string): number {
  return value.replace(/\s+/g, '').length;
}
