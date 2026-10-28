import React from 'react';
import ReactDOM from 'react-dom/client';
import {
  Navigate,
  RouterProvider,
  createBrowserRouter,
  useLocation,
} from 'react-router-dom';
import { HelmetProvider } from 'react-helmet-async';
import NotFound from './pages/404';
import ReactGA from 'react-ga4';
import {
  GOOGLE_ANALYTICS_TRACKING_ID,
  USE_GOOGLE_ANALYTICS,
} from './utils/const';
import '@/styles/index.css';
import { withOptionalGAPageTracking } from './utils/trackRoute';

const Dashboard = React.lazy(() => import('@/pages/dashboard'));

const DashboardRedirect = ({ summary = false }: { summary?: boolean }) => {
  const { search, hash } = useLocation();
  return (
    <Navigate
      to={{ pathname: summary ? '/summary' : '/', search, hash }}
      replace
    />
  );
};

const dashboard = () => (
  <React.Suspense
    fallback={
      <div role="status" className="p-8">
        正在加载 Liang page…
      </div>
    }
  >
    <Dashboard />
  </React.Suspense>
);

if (USE_GOOGLE_ANALYTICS) {
  ReactGA.initialize(GOOGLE_ANALYTICS_TRACKING_ID);
}

const routes = createBrowserRouter(
  [
    {
      path: '/',
      element: withOptionalGAPageTracking(dashboard()),
    },
    {
      path: 'classic',
      element: <DashboardRedirect />,
    },
    {
      path: 'dashboard',
      element: <DashboardRedirect />,
    },
    {
      path: 'dashboard/summary',
      element: <DashboardRedirect summary />,
    },
    {
      path: 'summary',
      element: withOptionalGAPageTracking(dashboard()),
    },
    {
      path: 'classic/summary',
      element: <DashboardRedirect summary />,
    },
    {
      path: '*',
      element: withOptionalGAPageTracking(<NotFound />),
    },
  ],
  { basename: import.meta.env.BASE_URL }
);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <HelmetProvider>
      <RouterProvider router={routes} />
    </HelmetProvider>
  </React.StrictMode>
);
