export type OutlineItem = {
  id: string;
  title: string;
  level: number;
  anchor: string;
  children?: OutlineItem[];
};

export type TitleNode = {
  type: 'title';
  id: string;
  section_id: string;
  text: string;
};

export type ParagraphNode = {
  type: 'paragraph';
  id: string;
  section_id: string;
  text: string;
};

export type ListNode = {
  type: 'list';
  id: string;
  section_id: string;
  ordered: boolean;
  items: string[];
};

export type ImageNode = {
  type: 'image';
  id: string;
  section_id: string;
  src: string;
  caption?: string;
  alt?: string;
};

export type TableNode = {
  type: 'table';
  id: string;
  section_id: string;
  columns: string[];
  rows: string[][];
};

export type FormulaNode = {
  type: 'formula';
  id: string;
  section_id: string;
  latex: string;
};

export type FigureRenderAsset = {
  figure_id: string;
  label: string;
  title: string;
  source?: {
    type: string;
    content: string;
  };
};

export type FigureNode = {
  type: 'figure';
  id: string;
  section_id: string;
  figure_id: string;
};

export type SectionNode = {
  type: 'section';
  id: string;
  title: string;
  level: number;
  anchor: string;
  children: RenderNode[];
};

export type RenderNode = TitleNode | ParagraphNode | ListNode | ImageNode | TableNode | FormulaNode | FigureNode | SectionNode;

export type RenderAst = {
  type: 'document';
  title: string;
  meta: {
    document_type: string;
    schema_version: string;
  };
  figures?: FigureRenderAsset[];
  outline: OutlineItem[];
  children: RenderNode[];
};
