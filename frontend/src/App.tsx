import React from 'react';
import { BrowserRouter as Router, Routes, Route, Link } from 'react-router-dom';
import { Dashboard } from './pages/Dashboard';
import { TradeHistory } from './pages/TradeHistory';
import { AIInsights } from './pages/AIInsights';
import { Controls } from './pages/Controls';
import { Activity, TrendingUp, Brain, Settings } from 'lucide-react';

function App() {
  return (
    <Router>
      <div className="min-h-screen bg-gray-50">
        <nav className="bg-white border-b shadow-sm">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div className="flex justify-between h-16">
              <div className="flex">
                <Link to="/" className="flex items-center px-2 py-2 text-xl font-bold text-primary">
                  AI Trading Bot
                </Link>
                <div className="hidden sm:ml-6 sm:flex sm:space-x-8">
                  <Link
                    to="/"
                    className="inline-flex items-center px-1 pt-1 text-sm font-medium text-gray-900 hover:text-primary"
                  >
                    <Activity className="h-4 w-4 mr-1" />
                    Dashboard
                  </Link>
                  <Link
                    to="/history"
                    className="inline-flex items-center px-1 pt-1 text-sm font-medium text-gray-900 hover:text-primary"
                  >
                    <TrendingUp className="h-4 w-4 mr-1" />
                    History
                  </Link>
                  <Link
                    to="/insights"
                    className="inline-flex items-center px-1 pt-1 text-sm font-medium text-gray-900 hover:text-primary"
                  >
                    <Brain className="h-4 w-4 mr-1" />
                    AI Insights
                  </Link>
                  <Link
                    to="/controls"
                    className="inline-flex items-center px-1 pt-1 text-sm font-medium text-gray-900 hover:text-primary"
                  >
                    <Settings className="h-4 w-4 mr-1" />
                    Controls
                  </Link>
                </div>
              </div>
            </div>
          </div>
        </nav>

        <main className="max-w-7xl mx-auto py-6 sm:px-6 lg:px-8">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/history" element={<TradeHistory />} />
            <Route path="/insights" element={<AIInsights />} />
            <Route path="/controls" element={<Controls />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;

