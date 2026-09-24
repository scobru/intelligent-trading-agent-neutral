/**
 * SynFutures Perp Service - REST API Server
 * 
 * Exposes the SynFutures SDK as HTTP endpoints for the Python trading bot.
 */

import express, { Request, Response, NextFunction } from 'express';
import cors from 'cors';
import dotenv from 'dotenv';
import { ethers } from 'ethers';
import path from 'path';
import { SynFuturesService } from './synfutures';

// Load environment variables
dotenv.config(); // Loads .env from current working directory
dotenv.config({ path: path.resolve(__dirname, '../../.env') }); // Falls back to root .env if running from subdirectory

export const app = express();
const PORT = process.env.SYNFUTURES_PORT || 3100;

// Middleware
const allowedOrigins = [(process.env.CORS_ORIGIN || 'http://localhost')];
app.use(cors({
    origin: (origin, callback) => {
        // Allow requests with no origin (like mobile apps or curl requests)
        if (!origin) return callback(null, true);
        if (allowedOrigins.indexOf(origin) !== -1 || allowedOrigins.includes('*')) {
            callback(null, true);
        } else {
            callback(new Error('Not allowed by CORS'));
        }
    }
}));

// API Key Authentication Middleware
app.use((req: Request, res: Response, next: NextFunction) => {
    // Skip auth for health check endpoint if desired, or require it.
    // Usually health checks don't need auth.
    if (req.path === '/health') {
        return next();
    }

    const apiKey = process.env.API_KEY;
    if (apiKey) {
        const clientApiKey = req.headers['x-api-key'];
        if (!clientApiKey || clientApiKey !== apiKey) {
            return res.status(401).json({ error: 'Unauthorized' });
        }
    }

    next();
});

app.use(express.json());

// Initialize SynFutures service
const rpcUrl = process.env.BASE_RPC || process.env.BASE_RPC_URL || 'https://mainnet.base.org';
const privateKey = process.env.SYNFUTURES_PRIVATE_KEY;

const synfutures = new SynFuturesService(rpcUrl, privateKey);

// Error handler wrapper
const asyncHandler = (fn: (req: Request, res: Response, next: NextFunction) => Promise<any>) => 
    (req: Request, res: Response, next: NextFunction) => {
        Promise.resolve(fn(req, res, next)).catch(next);
    };

// ============================================================================
// ROUTES
// ============================================================================

/**
 * Health check
 */
app.get('/health', (req, res) => {
    res.json({
        status: 'ok',
        chain: 'base',
        signerConfigured: synfutures.getSignerAddress() !== null,
        signerAddress: synfutures.getSignerAddress(),
    });
});

/**
 * Get all instruments
 */
app.get('/instruments', asyncHandler(async (req, res) => {
    const instruments = await synfutures.getAllInstruments();
    
    // Serialize for JSON (Maps and BigNumbers need conversion)
    // InstrumentModel keeps symbol/addr/base/quote under .info and pairs under .pairs (Map)
    const serialized = instruments.map((inst: any) => {
        const info = inst.info || inst;
        return {
            instrumentAddr: info.addr,
            symbol: info.symbol,
            spotPrice: inst.spotPrice?.toString() || '0',
            base: info.base,
            quote: info.quote,
            minTradeValue: inst.minTradeValue?.toString() || '0',
            amms: Object.fromEntries(
                Array.from((inst.pairs as Map<number, any>)?.entries() || []).map((entry: unknown) => {
                    const [expiry, pair] = entry as [number, any];
                    return [
                        expiry.toString(),
                        {
                            expiry,
                            markPrice: pair.markPrice ? ethers.utils.formatEther(pair.markPrice) : '0',
                            fairPrice: pair.fairPriceWad ? ethers.utils.formatEther(pair.fairPriceWad) : '0',
                        },
                    ];
                })
            ),
        };
    });

    res.json(serialized);
}));

/**
 * Funding dei perpetual (prezzi spot/mark/fair e open interest long/short)
 */
app.get('/funding', asyncHandler(async (req, res) => {
    res.json(await synfutures.getFunding());
}));

/**
 * Get specific instrument by symbol
 */
app.get('/instrument/:symbol', asyncHandler(async (req, res) => {
    const { symbol } = req.params;
    const instrument = await synfutures.getInstrument(symbol);
    
    if (!instrument) {
        return res.status(404).json({ error: `Instrument ${symbol} not found` });
    }

    // The SDK instrument object has properties on instrument.info
    const info = (instrument as any).info || instrument;
    
    // Try to get pair model for perpetual expiry
    let markPrice = '0';
    let fairPrice = '0';
    try {
        const PERP_EXPIRY = 4294967295; // From @synfutures/oyster-sdk
        const pair = (instrument as any).getPairModel?.(PERP_EXPIRY);
        if (pair) {
            // Get mark price from pair AMM
            markPrice = pair.markPrice ? ethers.utils.formatEther(pair.markPrice) : '0';
            fairPrice = pair.fairPriceWad ? ethers.utils.formatEther(pair.fairPriceWad) : '0';
        }
    } catch (e) {
        console.log(`Note: Could not get pair model for ${symbol}:`, e instanceof Error ? e.message : e);
    }

    // Serialize
    const serialized = {
        instrumentAddr: info.addr || info.instrumentAddr || (instrument as any).instrumentAddr,
        symbol: info.symbol || symbol,
        spotPrice: info.spotPrice?.toString() || '0',
        base: info.base,
        quote: info.quote,
        minTradeValue: info.minTradeValue?.toString() || '0',
        amms: {
            '4294967295': {
                expiry: 4294967295,
                markPrice,
                fairPrice,
            }
        },
    };

    console.log(`[DATA] Instrument ${symbol}: markPrice=${markPrice}, fairPrice=${fairPrice}`);
    res.json(serialized);
}));

/**
 * Get Gate info (contract address, supported tokens)
 */
app.get('/gate/info', asyncHandler(async (req, res) => {
    const info = await synfutures.getGateInfo();
    res.json(info);
}));

/**
 * Get Gate balances for an address
 */
app.get('/gate/balance/:address', asyncHandler(async (req, res) => {
    const { address } = req.params;
    const balances = await synfutures.getGateBalances(address);
    
    const serialized = balances.map((b: any) => ({
        symbol: b.symbol,
        address: b.address,
        balance: ethers.utils.formatUnits(b.balance, b.decimals),
        decimals: b.decimals,
    }));

    res.json(serialized);
}));

/**
 * Get portfolio (positions) for an address
 */
app.get('/portfolio/:address', asyncHandler(async (req, res) => {
    const { address } = req.params;
    console.log(`[DATA] Fetching portfolios for ${address}...`);
    
    const portfolios = await synfutures.getAllPortfolios(address);
    
    // Debug: log raw portfolio data
    console.log(`[DATA] Found ${portfolios.length} portfolios`);
    for (const p of portfolios) {
        const pos = p.position;
        if (pos && !pos.size.isZero?.()) {
            console.log(`[DATA] NON-ZERO POSITION FOUND!`);
            console.log(`   instrumentAddr: ${p.instrumentAddr}`);
            console.log(`   symbol: ${p.symbol}`);
            console.log(`   size: ${pos.size.toString()}`);
            console.log(`   side: ${pos.side}`);
            // Log all keys to see what's available
            console.log(`   position keys: ${Object.keys(pos).join(', ')}`);
            if (p.accountRaw) {
                console.log(`   account keys: ${Object.keys(p.accountRaw).join(', ')}`);
                console.log(`   account details:`, JSON.parse(JSON.stringify(p.accountRaw)));
            }
        }
    }
    
    const serialized = portfolios.map((p: any) => {
        const pos = p.position || {};
        const acc = p.accountRaw || {};
        
        // Try multiple sources for margin, prioritizing explicitly named margin fields
        const marginValue = pos.margin?.toString() 
            || acc.margin 
            || acc.lockedMargin 
            || acc.altMargin 
            || pos.balance?.toString() // Sometimes 'balance' in position acts as margin
            || acc.balance 
            || '0';

        // Calculate entry price if not explicitly provided
        let entryPrice = pos.entryPrice?.toString();
        if ((!entryPrice || entryPrice === '0') && pos.entryNotional && pos.size && !pos.size.isZero()) {
            try {
                // entryPrice = abs(entryNotional / size)
                // Both are BigNumbers (wad)
                const entryNotional = ethers.BigNumber.from(pos.entryNotional).abs();
                const size = ethers.BigNumber.from(pos.size).abs();
                if (!size.isZero()) {
                    // (entryNotional * 1e18) / size to keep precision in wad
                    entryPrice = entryNotional.mul(ethers.utils.parseEther('1')).div(size).toString();
                }
            } catch (e) {
                entryPrice = '0';
            }
        }

        return {
            instrumentAddr: p.instrumentAddr,
            symbol: p.symbol,
            expiry: p.expiry,
            position: {
                size: pos.size?.toString() || '0',
                side: pos.side || 'FLAT',
                entryPrice: entryPrice || '0',
                margin: marginValue,
                markPrice: p.markPrice || '0', // Include markPrice if we can get it
            },
            orders: (Array.isArray(p.orders) 
                ? p.orders 
                : Array.from((p.orders as any)?.values?.() || [])
            ).map((o: any) => ({
                oid: o.oid,
                size: o.size?.toString() || '0',
                tick: o.tick,
                side: o.side,
            })),
        };
    });

    res.json(serialized);
}));

/**
 * Place market order
 * POST /order/market
 * Body: { symbol: string, side: "LONG" | "SHORT", sizeUsd: number, leverage: number, slippage?: number }
 */
app.post('/order/market', asyncHandler(async (req, res) => {
    const { symbol, side, sizeUsd, leverage, slippage } = req.body;

    if (!symbol || !side || !sizeUsd || !leverage) {
        return res.status(400).json({ 
            error: 'Missing required fields: symbol, side, sizeUsd, leverage' 
        });
    }

    if (!['LONG', 'SHORT'].includes(side.toUpperCase())) {
        return res.status(400).json({ error: 'side must be LONG or SHORT' });
    }

    const result = await synfutures.placeMarketOrder({
        instrumentSymbol: symbol,
        side: side.toUpperCase() as 'LONG' | 'SHORT',
        sizeUsd: parseFloat(sizeUsd),
        leverage: parseFloat(leverage),
        slippage: slippage ? parseInt(slippage) : undefined,
    });

    res.json(result);
}));

/**
 * Close position
 * POST /order/close
 * Body: { symbol: string }
 */
app.post('/order/close', asyncHandler(async (req, res) => {
    const { symbol } = req.body;

    if (!symbol) {
        return res.status(400).json({ error: 'Missing required field: symbol' });
    }

    const result = await synfutures.closePosition(symbol);
    res.json(result);
}));

/**
 * Deposit to Gate
 * POST /gate/deposit
 * Body: { token: string, amount: string }
 */
app.post('/gate/deposit', asyncHandler(async (req, res) => {
    const { token, amount } = req.body;

    if (!token || !amount) {
        return res.status(400).json({ error: 'Missing required fields: token, amount' });
    }

    const result = await synfutures.deposit(token, amount);
    res.json(result);
}));

/**
 * Withdraw from Gate
 * POST /gate/withdraw
 * Body: { token: string, amount: string }
 */
app.post('/gate/withdraw', asyncHandler(async (req, res) => {
    const { token, amount } = req.body;

    if (!token || !amount) {
        return res.status(400).json({ error: 'Missing required fields: token, amount' });
    }

    const result = await synfutures.withdraw(token, amount);
    res.json(result);
}));

// ============================================================================
// LIQUIDITY PROVISION ENDPOINTS
// ============================================================================

/**
 * Add liquidity to AMM
 */
app.post('/lp/add', asyncHandler(async (req, res) => {
    const { instrumentSymbol, alphaWad, marginAmount, slippage } = req.body;

    if (!instrumentSymbol || !alphaWad || !marginAmount) {
        return res.status(400).json({
            error: 'Missing required fields: instrumentSymbol, alphaWad, marginAmount',
        });
    }

    const result = await synfutures.addLiquidity({
        instrumentSymbol,
        alphaWad,
        marginAmount,
        slippage,
    });

    res.json(result);
}));

/**
 * Add asymmetric liquidity to AMM (different ranges above/below current price)
 */
app.post('/lp/add-asymmetric', asyncHandler(async (req, res) => {
    const { instrumentSymbol, lowerPercent, upperPercent, marginAmount, slippage } = req.body;

    if (!instrumentSymbol || lowerPercent === undefined || upperPercent === undefined || !marginAmount) {
        return res.status(400).json({
            error: 'Missing required fields: instrumentSymbol, lowerPercent, upperPercent, marginAmount',
        });
    }

    const result = await synfutures.addAsymmetricLiquidity({
        instrumentSymbol,
        lowerPercent: parseFloat(lowerPercent),
        upperPercent: parseFloat(upperPercent),
        marginAmount,
        slippage,
    });

    res.json(result);
}));

/**
 * Remove liquidity from AMM
 */
app.post('/lp/remove', asyncHandler(async (req, res) => {
    const { instrumentSymbol, rangeId, slippage } = req.body;

    if (!instrumentSymbol) {
        return res.status(400).json({
            error: 'Missing required field: instrumentSymbol',
        });
    }

    const result = await synfutures.removeLiquidity({
        instrumentSymbol,
        rangeId,
        slippage,
    });

    res.json(result);
}));

/**
 * Get LP positions for an address
 */
app.get('/lp/positions/:address', asyncHandler(async (req, res) => {
    const { address } = req.params;
    const positions = await synfutures.getLPPositions(address);
    res.json(positions);
}));

// ============================================================================
// ERROR HANDLING
// ============================================================================

app.use((err: Error, req: Request, res: Response, next: NextFunction) => {
    console.error('[ERR] Error:', err.message);
    res.status(500).json({
        error: err.message,
        stack: process.env.NODE_ENV === 'development' ? err.stack : undefined,
    });
});

// ============================================================================
// START SERVER
// ============================================================================

async function main() {
    try {
        console.log('[START] Starting SynFutures service...');
        console.log(`   RPC: ${rpcUrl}`);
        console.log(`   Private key configured: ${privateKey ? 'Yes' : 'No'}`);
        
        // Initialize SDK
        await synfutures.init();
        
        // Start server
        app.listen(PORT, () => {
            console.log(`[OK] SynFutures service running on http://localhost:${PORT}`);
            console.log('\n[INFO] Available endpoints:');
            console.log('   GET  /health              - Health check');
            console.log('   GET  /instruments         - Get all trading pairs');
            console.log('   GET  /instrument/:symbol  - Get specific instrument');
            console.log('   GET  /gate/balance/:addr  - Get Gate balances');
            console.log('   GET  /portfolio/:addr     - Get positions');
            console.log('   POST /order/market        - Place market order');
            console.log('   POST /order/close         - Close position');
            console.log('   POST /gate/deposit        - Deposit to Gate');
            console.log('   POST /gate/withdraw       - Withdraw from Gate');
            console.log('   POST /lp/add              - Add liquidity');
            console.log('   POST /lp/remove           - Remove liquidity');
            console.log('   GET  /lp/positions/:addr  - Get LP positions');
        });
    } catch (error) {
        console.error('[ERR] Failed to start service:', error);
        process.exit(1);
    }
}

if (require.main === module) { main(); }
