<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tulia House Solar</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js"></script>
    <style>
        :root {
            --bg: #0a0e13;
            --surface: #151922;
            --surface-2: #1d232e;
            --border: rgba(58, 70, 89, 0.5);
            --text: #e6edf5;
            --text-muted: #8a95a8;
            --primary: #3fb950;
            --primary-hover: #4ed65e;
            --warning: #f0883e;
            --danger: #f85149;
            --info: #58a6ff;
            --battery-primary: #58a6ff;
            --battery-backup: #f0883e;
            --radius: 16px;
            --shadow-sm: 0 4px 8px -2px rgba(0, 0, 0, 0.2);
            --shadow-md: 0 8px 16px -3px rgba(0, 0, 0, 0.3);
            --shadow-lg: 0 12px 24px -4px rgba(0, 0, 0, 0.4);
            --transition: 0.3s cubic-bezier(0.4, 0.0, 0.2, 1);
        }
        
        * { 
            margin: 0; 
            padding: 0; 
            box-sizing: border-box; 
        }
        
        body {
            font-family: 'DM Sans', system-ui, -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            padding: 1.5rem;
        }
        
        /* Dashboard Grid System */
        .dashboard-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 1.5rem;
        }
        
        @media (min-width: 768px) {
            .dashboard-grid {
                grid-template-columns: repeat(12, 1fr);
            }
            .span-12 { grid-column: span 12; }
            .span-9 { grid-column: span 9; }
            .span-8 { grid-column: span 8; }
            .span-6 { grid-column: span 6; }
            .span-4 { grid-column: span 4; }
            .span-3 { grid-column: span 3; }
        }
        
        @media (min-width: 1024px) {
            .container { padding: 2rem; }
            .dashboard-grid { gap: 1.5rem; }
        }
        
        /* Header */
        header {
            text-align: center;
            padding: 1rem 0 2rem;
            grid-column: 1 / -1;
        }
        
        h1 {
            font-size: clamp(1.75rem, 5vw, 2.25rem);
            font-weight: 800;
            color: var(--primary);
            letter-spacing: -0.02em;
            font-family: 'Space Mono', monospace;
        }
        
        .subtitle {
            font-family: 'Space Mono', monospace;
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-top: 0.5rem;
        }
        
        /* Card Component */
        .card {
            background: var(--surface);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 1.5rem;
            transition: transform var(--transition), box-shadow var(--transition), border-color var(--transition);
            display: flex;
            flex-direction: column;
            position: relative;
            overflow: hidden;
            box-shadow: var(--shadow-md);
        }
        
        .card:hover {
            transform: translateY(-2px);
            border-color: rgba(63, 185, 80, 0.6);
            box-shadow: 0 16px 32px -6px rgba(0, 0, 0, 0.5);
        }

        .card h2 {
            font-size: 1.1rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: var(--text);
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        /* Status Hero */
        .status-hero {
            background: linear-gradient(135deg, var(--surface) 0%, var(--surface-2) 100%);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 2rem;
            text-align: center;
            position: relative;
            overflow: hidden;
            box-shadow: var(--shadow-lg);
        }
        
        .status-hero::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            opacity: 0.1;
            background-size: cover;
            pointer-events: none;
        }
        
        .status-hero.critical { 
            border-color: var(--danger); 
            background: linear-gradient(135deg, rgba(248,81,73,0.15), rgba(21,25,34,0.95)); 
        }
        .status-hero.warning { 
            border-color: var(--warning); 
            background: linear-gradient(135deg, rgba(240,136,62,0.15), rgba(21,25,34,0.95)); 
        }
        .status-hero.good { 
            border-color: var(--primary); 
            background: linear-gradient(135deg, rgba(63,185,80,0.15), rgba(21,25,34,0.95)); 
        }
        
        .status-title {
            font-size: clamp(1.5rem, 3vw, 2.5rem);
            font-weight: 800;
            margin: 0.5rem 0;
        }
        
        .status-hero.critical .status-title { color: var(--danger); }
        .status-hero.warning .status-title { color: var(--warning); }
        .status-hero.good .status-title { color: var(--primary); }
        .status-hero.normal .status-title { color: var(--info); }
        
        /* Metric Cards */
        .metric-label {
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 600;
        }
        
        .metric-value {
            font-size: clamp(1.5rem, 4vw, 1.875rem);
            font-weight: 600;
            font-family: 'Space Mono', monospace;
            margin: 0.25rem 0;
            letter-spacing: 0.02em;
            font-variant-numeric: tabular-nums;
        }
        
        .metric-unit { 
            font-size: 1rem; 
            font-weight: 400; 
            color: var(--text-muted); 
            margin-left: 2px; 
        }
        
        .text-success { color: var(--primary); }
        .text-warning { color: var(--warning); }
        .text-danger { color: var(--danger); }
        .text-info { color: var(--info); }
        
        /* Power Flow - FIXED WITH CSS GRID */
        .power-flow-container {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 300px;
            position: relative;
        }
        
        .power-flow {
            position: relative;
            width: 100%;
            max-width: 800px;
            height: 300px; /* Critical: Defined height for grid layout */
            aspect-ratio: 16/9;
            /* Use CSS Grid for stable positioning */
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            grid-template-rows: 1fr auto 1fr;
            align-items: center;
            justify-items: center;
            margin: 0 auto;
        }
        
        .flow-svg {
            position: absolute;
            width: 100%;
            height: 100%;
            top: 0; 
            left: 0;
            z-index: 1;
            pointer-events: none; /* Let clicks pass through to nodes */
        }
        
        .flow-node {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            background: var(--surface-2);
            border: 2px solid var(--border);
            border-radius: 50%;
            z-index: 10;
            box-shadow: var(--shadow-sm);
            transition: all var(--transition);
            width: clamp(60px, 14vw, 90px);
            height: clamp(60px, 14vw, 90px);
            position: relative; /* Changed from static/absolute to relative */
        }
        
        /* Position nodes in the grid with proper alignment */
        .flow-node.solar { 
            grid-column: 1; 
            grid-row: 2;
            justify-self: end; /* Align to right of grid cell */
            margin-right: 15px;
        }
        
        .flow-node.inverter { 
            grid-column: 2; 
            grid-row: 2;
            width: clamp(70px, 18vw, 110px);
            height: clamp(70px, 18vw, 110px);
            border-color: var(--info);
            box-shadow: var(--shadow-md);
        }
        
        .flow-node.load { 
            grid-column: 3; 
            grid-row: 2;
            justify-self: start; /* Align to left of grid cell */
            margin-left: 15px;
        }
        
        .flow-node.battery { 
            grid-column: 2; 
            grid-row: 3;
            align-self: start; /* Align to top of grid cell */
            margin-top: 15px;
        }
        
        .flow-node.generator { 
            grid-column: 2; 
            grid-row: 1;
            align-self: end; /* Align to bottom of grid cell */
            margin-bottom: 15px;
        }
        
        .flow-node-content {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            padding: 5px;
        }
        
        .flow-icon { 
            font-size: clamp(1.2rem, 3vw, 1.5rem); 
            margin-bottom: 2px; 
        }
        
        .flow-label { 
            font-size: clamp(0.5rem, 1.5vw, 0.65rem); 
            text-transform: uppercase; 
            color: var(--text-muted); 
            font-weight: 600; 
            text-align: center;
            line-height: 1.1;
        }
        
        .flow-value { 
            font-family: 'Space Mono', monospace; 
            font-weight: 700; 
            color: #fff; 
            font-size: clamp(0.7rem, 2vw, 0.85rem);
            text-align: center;
            line-height: 1.1;
        }

        /* Battery System - Simplified */
        .battery-system-card {
            box-shadow: var(--shadow-md);
        }
        
        .battery-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 1.5rem;
            gap: 1rem;
        }
        
        .battery-icon { font-size: 1.5rem; }
        
        .battery-title {
            font-size: 1.1rem;
            font-weight: 600;
            flex: 1;
        }
        
        .battery-total {
            font-family: 'Space Mono', monospace;
            font-size: 0.9rem;
            color: var(--text-muted);
        }
        
        .battery-combined-bar {
            position: relative;
            margin-bottom: 1.5rem;
        }
        
        .battery-bar-track {
            width: 100%;
            height: 32px;
            background: rgba(0, 0, 0, 0.3);
            border-radius: 8px;
            overflow: hidden;
            position: relative;
            border: 1px solid var(--border);
        }
        
        .battery-bar-fill {
            height: 100%;
            transition: width 1.5s ease;
            position: relative;
            background: linear-gradient(90deg, var(--battery-primary) 0%, var(--battery-backup) 100%);
        }
        
        .battery-bar-fill.success {
            background: linear-gradient(90deg, var(--battery-primary) 0%, var(--primary) 100%);
        }
        
        .battery-bar-fill.warning {
            background: linear-gradient(90deg, var(--warning) 0%, var(--battery-backup) 100%);
        }
        
        .battery-bar-fill.danger {
            background: linear-gradient(90deg, var(--danger) 0%, var(--warning) 100%);
        }
        
        .battery-percentage {
            position: absolute;
            right: 1rem;
            top: 50%;
            transform: translateY(-50%);
            font-family: 'Space Mono', monospace;
            font-weight: 700;
            font-size: 1.1rem;
            color: var(--text);
            text-shadow: 0 2px 4px rgba(0,0,0,0.8);
        }
        
        .battery-details {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            margin-bottom: 1rem;
        }
        
        .battery-source {
            padding: 1rem;
            background: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--border);
            border-radius: 8px;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }
        
        .battery-source.active {
            border-color: var(--battery-primary);
            box-shadow: 0 0 20px rgba(88, 166, 255, 0.3);
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { box-shadow: 0 0 20px rgba(88, 166, 255, 0.3); }
            50% { box-shadow: 0 0 30px rgba(88, 166, 255, 0.6); }
        }
        
        .source-label {
            font-size: 0.85rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 600;
        }
        
        .source-status {
            font-family: 'Space Mono', monospace;
            font-size: 0.9rem;
            color: var(--text);
        }
        
        .battery-footer {
            padding-top: 1rem;
            border-top: 1px solid var(--border);
        }
        
        .battery-info {
            font-size: 0.85rem;
            color: var(--text-muted);
            text-align: center;
            margin-bottom: 0.5rem;
        }
        
        .battery-runtime {
            font-size: 0.9rem;
            color: var(--text);
            text-align: center;
            font-weight: 500;
        }
        
        /* Recommendations */
        .rec-item {
            display: flex;
            align-items: flex-start;
            gap: 1rem;
            padding: 1rem;
            background: rgba(255,255,255,0.03);
            border-radius: 8px;
            margin-bottom: 0.75rem;
            border-left: 4px solid;
        }
        
        .rec-item.critical { border-left-color: var(--danger); }
        .rec-item.warning { border-left-color: var(--warning); }
        .rec-item.good { border-left-color: var(--primary); }
        .rec-item.normal { border-left-color: var(--info); }
        
        .rec-icon { font-size: 1.5rem; }
        .rec-title { font-weight: 600; margin-bottom: 0.25rem; }
        .rec-desc { font-size: 0.85rem; color: var(--text-muted); }
        
        /* Chart Containers */
        .chart-wrapper {
            position: relative;
            width: 100%;
            height: 280px;
        }
        
        @media (min-width: 768px) {
            .chart-wrapper { height: 320px; }
        }
        
        @media (min-width: 1024px) {
            .chart-wrapper { height: 400px; }
        }

        /* Inverters Grid */
        .inv-grid {
            display: grid;
            grid-template-columns: 1fr;
            gap: 1rem;
        }
        
        @media (min-width: 600px) {
            .inv-grid {
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            }
        }
        
        .inv-card {
            background: rgba(0,0,0,0.2);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 1rem;
        }
        
        .inv-card.fault { 
            border-color: var(--danger); 
            background: rgba(248,81,73,0.1); 
        }
        
        /* Alerts List */
        .alert-row {
            display: flex;
            gap: 1rem;
            padding: 0.75rem;
            border-bottom: 1px solid var(--border);
            font-size: 0.9rem;
        }
        .alert-row:last-child { border-bottom: none; }
        .alert-time { 
            font-family: 'Space Mono', monospace; 
            color: var(--text-muted);
            min-width: 50px;
        }
        
        /* Mobile Optimizations */
        @media (max-width: 767px) {
            .container { padding: 1rem; }
            .dashboard-grid { gap: 1rem; }
            .card { padding: 1rem; }
            .status-hero { padding: 1.5rem; }
            
            .battery-details {
                grid-template-columns: 1fr;
            }
            
            .power-flow {
                height: 250px; /* Adjust height for mobile */
            }
            
            .flow-node {
                width: clamp(50px, 16vw, 70px);
                height: clamp(50px, 16vw, 70px);
            }
            
            .flow-node.inverter {
                width: clamp(60px, 20vw, 85px);
                height: clamp(60px, 20vw, 85px);
            }
            
            /* Adjust margins for mobile */
            .flow-node.solar { margin-right: 8px; }
            .flow-node.load { margin-left: 8px; }
            .flow-node.battery { margin-top: 8px; }
            .flow-node.generator { margin-bottom: 8px; }
        }
        
        /* Focus styles for accessibility */
        *:focus-visible {
            outline: 2px solid var(--info);
            outline-offset: 2px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="dashboard-grid">
            <header>
                <h1>TULIA HOUSE SOLAR</h1>
                <div class="subtitle">2026-01-12 05:20:29 EAT</div>
            </header>
            
            <!-- Status Hero -->
            <div class="span-12 status-hero normal">
                <div style="font-size: 3rem; margin-bottom: 0.5rem">ℹ️</div>
                <div class="status-title">ℹ️ NORMAL</div>
                <div style="font-size: 1.1rem; opacity: 0.9">System running</div>
            </div>
            
            <!-- Key Metrics (Row of 4) -->
            <div class="card span-3">
                <div class="metric-label">Current Load</div>
                <div class="metric-value text-info">679<span class="metric-unit">W</span></div>
                <div style="font-size: 0.85rem; color: var(--text-muted)">↓ Low demand</div>
            </div>
            
            <div class="card span-3">
                <div class="metric-label">Solar Output</div>
                <div class="metric-value text-success">1<span class="metric-unit">W</span></div>
                <div style="font-size: 0.85rem; color: var(--text-muted)">☁️ Low production</div>
            </div>
            
            <div class="card span-3">
                <div class="metric-label">Primary Battery</div>
                <div class="metric-value text-warning">50<span class="metric-unit">%</span></div>
                <div style="font-size: 0.85rem; color: var(--text-muted)">Raw system reading</div>
            </div>
            
            <div class="card span-3">
                <div class="metric-label">Backup Voltage</div>
                <div class="metric-value text-success">53.2<span class="metric-unit">V</span></div>
                <div style="font-size: 0.85rem; color: var(--text-muted)">Status: Good</div>
            </div>
            
            <!-- Power Flow Diagram (Larger - span-9) -->
            <div class="card span-9">
                <h2>⚡ Real-Time Energy Flow</h2>
                <div class="power-flow-container">
                    <div class="power-flow">
                        <svg class="flow-svg" viewBox="0 0 100 56.25" preserveAspectRatio="xMidYMid meet">
                            <!-- Solar to Inverter -->
                            <defs>
                                <filter id="glow">
                                    <feGaussianBlur stdDeviation="2" result="coloredBlur"/>
                                    <feMerge>
                                        <feMergeNode in="coloredBlur"/>
                                        <feMergeNode in="SourceGraphic"/>
                                    </feMerge>
                                </filter>
                                <linearGradient id="solarGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                                    <stop offset="0%" style="stop-color:#3fb950;stop-opacity:1" />
                                    <stop offset="100%" style="stop-color:#58a6ff;stop-opacity:1" />
                                </linearGradient>
                                <linearGradient id="loadGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                                    <stop offset="0%" style="stop-color:#58a6ff;stop-opacity:1" />
                                    <stop offset="100%" style="stop-color:#3fb950;stop-opacity:1" />
                                </linearGradient>
                            </defs>
                            
                            <!-- Solar to Inverter -->
                            <path d="M 8 28.125 L 50 28.125" 
                                  stroke="var(--border)" 
                                  stroke-width="0.5"
                                  filter="" />
                            
                            
                            <!-- Inverter to Load -->
                            <path d="M 50 28.125 L 92 28.125" 
                                  stroke="url(#loadGradient)" 
                                  stroke-width="2"
                                  filter="url(#glow)" />
                            
                            <circle r="1" fill="var(--info)">
                                <animateMotion dur="1.5s" repeatCount="indefinite" path="M 50 28.125 L 92 28.125" />
                            </circle>
                            
                            
                            <!-- Battery to/from Inverter -->
                            <path d="M 50 28.125 L 50 52" 
                                  stroke="var(--danger)" 
                                  stroke-width="2"
                                  filter="url(#glow)" />
                            
                            <circle r="1" fill="var(--danger)">
                                <animateMotion dur="2s" repeatCount="indefinite" path="M 50 52 L 50 28.125" />
                            </circle>
                            
                            
                            <!-- Generator to Inverter -->
                            <path d="M 50 4 L 50 28.125" 
                                  stroke="var(--border)" 
                                  stroke-width="0.5"
                                  filter="" />
                            
                        </svg>
                        
                        <!-- DOM Nodes positioned with CSS Grid -->
                        <div class="flow-node solar"><div class="flow-node-content"><div class="flow-icon">☀️</div><div class="flow-label">Solar</div><div class="flow-value">1W</div></div></div>
                        <div class="flow-node inverter"><div class="flow-node-content"><div class="flow-icon">⚡</div><div class="flow-label">Inverter</div><div class="flow-value">25°C</div></div></div>
                        <div class="flow-node load"><div class="flow-node-content"><div class="flow-icon">🏠</div><div class="flow-label">Load</div><div class="flow-value">679W</div></div></div>
                        <div class="flow-node battery"><div class="flow-node-content"><div class="flow-icon">🔋</div><div class="flow-label">Bat</div><div class="flow-value">50%</div></div></div>
                        <div class="flow-node generator"><div class="flow-node-content"><div class="flow-icon">🔌</div><div class="flow-label">Gen</div><div class="flow-value">OFF</div></div></div>
                    </div>
                </div>
            </div>
            
            <!-- Battery Detail (Simplified - span-3) -->
            <div class="card battery-system-card span-3">
                <div class="battery-header">
                    <span class="battery-icon">🔋</span>
                    <span class="battery-title">BATTERY</span>
                </div>
                
                <div class="battery-combined-bar">
                    <div class="battery-bar-track">
                        <div class="battery-bar-fill warning" style="width: 49.6%"></div>
                    </div>
                    <div class="battery-percentage">50%</div>
                </div>
                
                <div class="battery-details">
                    <div class="battery-source active">
                        <span class="source-label">Primary</span>
                        <span class="source-status">⚡ Active • 757W</span>
                    </div>
                    
                    <div class="battery-source ">
                        <span class="source-label">Backup</span>
                        <span class="source-status">💤 Standby</span>
                    </div>
                </div>
                
                <div class="battery-footer">
                    <div class="battery-info">Backup activates when Primary reaches 40%</div>
                    <div class="battery-runtime">~22 hours remaining</div>
                </div>
            </div>

            <!-- Recommendations -->
            <div class="card span-4">
                <h2>📝 Recommendations</h2>
                
                <div class="rec-item warning">
                    <div class="rec-icon">⚠️</div>
                    <div>
                        <div class="rec-title">CONSERVE POWER</div>
                        <div class="rec-desc">Battery low (50%) and not charging well</div>
                    </div>
                </div>
                
            </div>

            <!-- Inverters -->
            <div class="card span-4">
                <h2>⚙️ Inverter Status</h2>
                <div class="inv-grid">
                
                    <div class="inv-card ">
                        <div style="font-weight: 700; font-size: 0.9rem; margin-bottom: 0.5rem">Inverter 1</div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Out:</span>
                            <span style="font-family:'Space Mono'">409W</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Bat:</span>
                            <span style="font-family:'Space Mono'">52.2V</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem;">
                            <span style="color:var(--text-muted)">Temp:</span>
                            <span class="text-success">21°C</span>
                        </div>
                    </div>
                
                    <div class="inv-card ">
                        <div style="font-weight: 700; font-size: 0.9rem; margin-bottom: 0.5rem">Inverter 2</div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Out:</span>
                            <span style="font-family:'Space Mono'">270W</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Bat:</span>
                            <span style="font-family:'Space Mono'">52.4V</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem;">
                            <span style="color:var(--text-muted)">Temp:</span>
                            <span class="text-success">28°C</span>
                        </div>
                    </div>
                
                    <div class="inv-card ">
                        <div style="font-weight: 700; font-size: 0.9rem; margin-bottom: 0.5rem">Inverter 3 (Backup)</div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Out:</span>
                            <span style="font-family:'Space Mono'">0W</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem; margin-bottom: 4px;">
                            <span style="color:var(--text-muted)">Bat:</span>
                            <span style="font-family:'Space Mono'">53.2V</span>
                        </div>
                        <div style="display:flex; justify-content:space-between; font-size: 0.8rem;">
                            <span style="color:var(--text-muted)">Temp:</span>
                            <span class="text-success">25°C</span>
                        </div>
                    </div>
                
                </div>
            </div>
            
            <!-- Schedule -->
            <div class="card span-4">
                 <h2>📅 Schedule</h2>
                 
                 <div class="rec-item good" style="border-left: 3px solid var(--primary)">
                    <div class="rec-icon">🚿</div>
                    <div>
                        <div class="rec-title">Best Time for Heavy Loads</div>
                        <div class="rec-desc">10:20 AM - 4:20 PM</div>
                    </div>
                 </div>
                 
            </div>
            
            <!-- Charts -->
            <div class="card span-6">
                <h2>🔮 12-Hour Forecast</h2>
                <div class="chart-wrapper">
                    <canvas id="forecastChart"></canvas>
                </div>
            </div>
            
            <div class="card span-6">
                <h2>🔋 Capacity Prediction</h2>
                <div class="chart-wrapper">
                    <canvas id="predictionChart"></canvas>
                </div>
            </div>
            
            <div class="card span-12">
                <h2>📉 14-Day History</h2>
                <div class="chart-wrapper">
                    <canvas id="historyChart"></canvas>
                </div>
            </div>

            <!-- Alerts -->
            <div class="card span-12">
                <h2>🔔 Recent Alerts</h2>
                
                    <div style="padding: 1rem; color: var(--text-muted); text-align: center;">No active alerts</div>
                
            </div>
        </div>
    </div>
    
    <script>
        // Chart Config
        Chart.defaults.color = '#8a95a8';
        Chart.defaults.borderColor = 'rgba(58, 70, 89, 0.4)';
        Chart.defaults.font.family = "'DM Sans', sans-serif";
        
        const commonOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: 'top', align: 'end', labels: { boxWidth: 10, usePointStyle: true, font: { size: 11 } } }
            },
            interaction: { mode: 'index', intersect: false }
        };

        // Forecast
        new Chart(document.getElementById('forecastChart'), {
            type: 'line',
            data: {
                labels: ["05:20", "06:20", "07:20", "08:20", "09:20", "10:20", "11:20", "12:20", "13:20", "14:20", "15:20", "16:20"],
                datasets: [
                    { 
                        label: 'Solar', 
                        data: [0, 0, 3.4076832368170056, 268.01582465331785, 1154.955204654697, 2924.591951124835, 4874.84796590103, 5118.0431644011005, 5193.431688917647, 5290.993523965752, 3914.808123552929, 1954.8270939624806], 
                        borderColor: '#3fb950', 
                        backgroundColor: 'rgba(63, 185, 80, 0.15)', 
                        fill: true, 
                        tension: 0.4,
                        borderWidth: 2
                    },
                    { 
                        label: 'Load', 
                        data: [903.2, 1239.5, 1575.8, 1200, 1200, 1200, 1200, 1200, 1200, 1200, 1200, 1200], 
                        borderColor: '#58a6ff', 
                        backgroundColor: 'rgba(88, 166, 255, 0.15)', 
                        fill: true, 
                        tension: 0.4,
                        borderWidth: 2
                    }
                ]
            },
            options: commonOptions
        });
        
        // Prediction
        new Chart(document.getElementById('predictionChart'), {
            type: 'line',
            data: {
                labels: ["Now", "05:20", "06:20", "07:20", "08:20", "09:20", "10:20", "11:20", "12:20", "13:20", "14:20", "15:20", "16:20"],
                datasets: [{
                    label: 'Capacity %',
                    data: [50.86206896551724, 48.266666666666666, 44.70488505747126, 40.18651633114028, 37.50840088474177, 37.37896181765756, 42.33468581514273, 52.89459376313419, 64.15333848842471, 75.62871690485473, 87.3844454219977, 95.18561819082795, 97.35466156428333],
                    borderColor: '#58a6ff',
                    borderWidth: 2,
                    segment: { 
                        borderColor: ctx => {
                            const y = ctx.p0.parsed.y;
                            if (y < 25) return '#f85149';
                            if (y < 60) return '#f0883e';
                            return '#3fb950';
                        }
                    },
                    fill: { target: 'origin', above: 'rgba(88, 166, 255, 0.1)' },
                    tension: 0.4
                }]
            },
            options: {
                ...commonOptions,
                plugins: { 
                    ...commonOptions.plugins, 
                    annotation: { 
                        annotations: {
                            line1: { 
                                type: 'line', 
                                yMin: 60, 
                                yMax: 60, 
                                borderColor: 'rgba(63, 185, 80, 0.5)', 
                                borderWidth: 2, 
                                borderDash: [4, 4],
                                label: {
                                    content: 'Safe Zone',
                                    enabled: true,
                                    position: 'end'
                                }
                            },
                            line2: {
                                type: 'line',
                                yMin: 25,
                                yMax: 25,
                                borderColor: 'rgba(240, 136, 62, 0.5)',
                                borderWidth: 2,
                                borderDash: [4, 4],
                                label: {
                                    content: 'Warning',
                                    enabled: true,
                                    position: 'end'
                                }
                            }
                        }
                    }
                }
            }
        });
        
        // History
        new Chart(document.getElementById('historyChart'), {
            type: 'line',
            data: {
                labels: ["12 Jan 05:20"],
                datasets: [
                    { 
                        label: 'Load', 
                        data: [679.0], 
                        borderColor: '#58a6ff', 
                        borderWidth: 2, 
                        pointRadius: 0,
                        tension: 0.3
                    },
                    { 
                        label: 'Discharge', 
                        data: [757.0], 
                        borderColor: '#f85149', 
                        borderWidth: 2, 
                        pointRadius: 0,
                        tension: 0.3
                    }
                ]
            },
            options: commonOptions
        });
        
        // Auto Refresh
        setInterval(() => {
            fetch('/api/data').then(r => r.json()).then(d => {
                if(d.timestamp !== "2026-01-12 05:20:29 EAT") location.reload();
            });
        }, 60000);
    </script>
</body>
</html>
