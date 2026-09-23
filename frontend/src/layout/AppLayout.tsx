import { Outlet } from 'react-router-dom'
import VSidebar from './VSidebar'
import VApiKeyBanner from '../components/VApiKeyBanner'

export default function AppLayout() {
  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-bg">
      <VApiKeyBanner />
      <div className="flex min-h-0 flex-1">
        <VSidebar />
        <main className="relative min-w-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
