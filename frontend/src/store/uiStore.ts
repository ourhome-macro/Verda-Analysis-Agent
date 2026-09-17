import { create } from 'zustand'

interface UIState {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebar: (v: boolean) => void
  /* 模型选择器（纯装饰，无副作用） */
  model: string
  setModel: (m: string) => void
}

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
  setSidebar: (v) => set({ sidebarCollapsed: v }),
  model: 'Auto',
  setModel: (m) => set({ model: m }),
}))
