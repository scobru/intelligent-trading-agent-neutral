/**
 * SynFutures Oyster SDK Wrapper
 * 
 * Provides a typed interface to the SynFutures V3 Oyster SDK for Base chain.
 * Uses the high-level intuitiveTrade function for reliable order execution.
 */

import { ethers } from 'ethers';

// Re-export types from oyster-sdk
export { Side, PERP_EXPIRY } from '@synfutures/oyster-sdk';

export interface GateBalance {
    symbol: string;
    address: string;
    balance: ethers.BigNumber;
    decimals: number;
}

/**
 * SynFutures SDK Service
 * 
 * Wraps the @synfutures/oyster-sdk for use in the REST API.
 */
export class SynFuturesService {
    private sdk: any = null;
    private initialized: boolean = false;
    private signer: ethers.Wallet | null = null;
    private instrumentCache: any[] | null = null;
    private lastCacheTime: number = 0;
    private readonly CACHE_DURATION = 5 * 60 * 1000; // 5 minutes

    constructor(
        private readonly rpcUrl: string,
        private readonly privateKey?: string
    ) {}

    /**
     * Initialize the SDK
     */
    async init(): Promise<void> {
        if (this.initialized) return;

        try {
            // Dynamic import for ESM compatibility
            const { SynFuturesV3 } = await import('@synfutures/oyster-sdk');

            // Initialize SDK for Base chain
            this.sdk = SynFuturesV3.getInstance('base');
            
            // Il provider va passato con setProvider(): l'SDK crea i contratti
            // (gate, observer, config) solo quando riceve un provider, e da
            // solo lo cerca nella variabile BASE_RPC. Assegnare ctx.provider a
            // mano lasciava i contratti vuoti ("reading 'gate'") se BASE_RPC
            // non era impostata.
            if (this.rpcUrl) {
                const provider = new ethers.providers.JsonRpcProvider(this.rpcUrl);
                this.sdk.setProvider(provider, true);
            }

            await this.sdk.init();

            // Set up signer if private key provided
            if (this.privateKey) {
                this.signer = new ethers.Wallet(this.privateKey, this.sdk.ctx.provider);
            }

            this.initialized = true;
            console.log('[OK] SynFutures Oyster SDK initialized for Base chain');
        } catch (error) {
            console.error('[ERR] Failed to initialize SynFutures SDK:', error);
            throw error;
        }
    }

    /**
     * Ensure SDK is initialized
     */
    private ensureInitialized(): void {
        if (!this.initialized || !this.sdk) {
            throw new Error('SynFutures SDK not initialized. Call init() first.');
        }
    }

    /**
     * Get signer or throw if not configured
     */
    private getSigner(): ethers.Wallet {
        if (!this.signer) {
            throw new Error('No signer configured. Set SYNFUTURES_PRIVATE_KEY.');
        }
        return this.signer;
    }

    /**
     * Get all available instruments (trading pairs) with caching
     */
    async getAllInstruments(): Promise<any[]> {
        this.ensureInitialized();
        
        const now = Date.now();
        if (this.instrumentCache && (now - this.lastCacheTime < this.CACHE_DURATION)) {
            return this.instrumentCache;
        }

        try {
            const instruments = await this.sdk.getAllInstruments();
            this.instrumentCache = instruments;
            this.lastCacheTime = now;
            return instruments;
        } catch (error) {
            console.error('Error fetching instruments:', error);
            // Return cache if available even if expired, otherwise throw
            if (this.instrumentCache) return this.instrumentCache;
            throw error;
        }
    }

    /**
     * Funding dei perpetual: tutto cio' che serve a stimare quanto incassa
     * (o paga) uno short. In SynFutures V3 il funding e' continuo: chi sta dal
     * lato "caro" paga |fair - mark| per unita' al giorno, diviso fra
     * chi sta dall'altro lato in proporzione all'open interest.
     */
    async getFunding(): Promise<any[]> {
        this.ensureInitialized();
        const { PERP_EXPIRY } = await import('@synfutures/oyster-sdk');
        const instruments = await this.getAllInstruments();
        const out: any[] = [];
        for (const inst of instruments) {
            const info = inst.info || inst;
            try {
                const pair = (inst.pairs as Map<number, any>)?.get(PERP_EXPIRY);
                if (!pair) continue;
                const amm = pair.amm || pair.state?.amm || {};
                out.push({
                    symbol: info.symbol,
                    instrumentAddr: info.addr,
                    spotPrice: inst.spotPrice ? ethers.utils.formatEther(inst.spotPrice) : null,
                    markPrice: pair.markPrice ? ethers.utils.formatEther(pair.markPrice) : null,
                    fairPrice: pair.fairPriceWad ? ethers.utils.formatEther(pair.fairPriceWad) : null,
                    totalLong: amm.totalLong ? ethers.utils.formatEther(amm.totalLong) : null,
                    totalShort: amm.totalShort ? ethers.utils.formatEther(amm.totalShort) : null,
                    openInterests: amm.openInterests ? ethers.utils.formatEther(amm.openInterests) : null,
                    fundingHour: inst.misc?.fundingHour ?? null,
                    timestamp: amm.timestamp ?? null,
                });
            } catch (error) {
                console.error(`Funding ${info.symbol} non disponibile:`, error);
            }
        }
        return out;
    }

    /**
     * Get a specific instrument by symbol
     */
    async getInstrument(symbol: string): Promise<any | null> {
        this.ensureInitialized();
        try {
            const instruments = await this.getAllInstruments();
            const instrument = instruments.find((i: any) => {
                const info = i.info || i;
                return info.symbol === symbol;
            });
            return instrument || null;
        } catch (error) {
            console.error(`Instrument ${symbol} not found:`, error);
            return null;
        }
    }

    /**
     * Get Gate balances for an address
     */
    async getGateBalances(address: string): Promise<GateBalance[]> {
        this.ensureInitialized();
        
        try {
            const quoteTokens = ['USDC', 'USDB'];
            const balancePromises = quoteTokens.map(async (symbol) => {
                try {
                    const tokenInfo = await this.sdk.ctx.getTokenInfo(symbol);
                    const balance = await this.sdk.contracts.gate.reserveOf(tokenInfo.address, address);
                    
                    if (balance && balance.gt(0)) {
                        return {
                            symbol,
                            address: tokenInfo.address,
                            balance,
                            decimals: tokenInfo.decimals,
                        };
                    }
                } catch {
                    // Skip
                }
                return null;
            });

            const results = await Promise.all(balancePromises);
            return results.filter((b): b is GateBalance => b !== null);
        } catch (error) {
            console.error('Error fetching gate balances:', error);
            return [];
        }
    }

    private async limitParallel<T, R>(
        items: T[],
        task: (item: T) => Promise<R>,
        limit: number = 5
    ): Promise<R[]> {
        const results: R[] = [];
        const executing: Promise<any>[] = [];

        for (const item of items) {
            const p = task(item).then(res => {
                executing.splice(executing.indexOf(p), 1);
                return res;
            });
            results.push(p as any);
            executing.push(p);

            if (executing.length >= limit) {
                await Promise.race(executing);
            }
        }
        return Promise.all(results);
    }

    /**
     * Get portfolio for an address
     */
    async getAllPortfolios(address: string): Promise<any[]> {
        this.ensureInitialized();
        const { PERP_EXPIRY } = await import('@synfutures/oyster-sdk');
        const instruments = await this.getAllInstruments();
        
        const fetchPortfolio = async (instrument: any) => {
            const info = instrument.info || instrument;
            try {
                const account = await this.sdk.getPairLevelAccount(address, info.addr, PERP_EXPIRY);
                if (account && account.position && !account.position.size.isZero()) {
                    console.log(`[DATA] RAW ACCOUNT for ${info.symbol}:`, Object.keys(account).join(', '));
                    
                    // The SDK structure has nested account details
                    const accDetails = account.account || account;
                    const pos = accDetails.position || account.position || {};

                    if (accDetails.position) {
                        console.log(`[DATA] Found nested position in account.account for ${info.symbol}`);
                    }

                    // Get mark price via InstrumentModel.pairs Map (instrument.amms does not exist)
                    let markPrice = '0';
                    try {
                        const perpPair = (instrument.pairs as Map<number, any>)?.get(PERP_EXPIRY);
                        if (perpPair?.markPrice) {
                            markPrice = perpPair.markPrice.toString();
                        }
                    } catch (e) {}

                    return {
                        instrumentAddr: info.addr,
                        symbol: info.symbol,
                        expiry: PERP_EXPIRY,
                        position: account.position,
                        markPrice: markPrice,
                        accountRaw: {
                            balance: accDetails.balance?.toString(),
                            margin: accDetails.margin?.toString(),
                            lockedMargin: accDetails.lockedMargin?.toString(),
                            availableBalance: accDetails.availableBalance?.toString(),
                            maintenanceMargin: accDetails.maintenanceMargin?.toString(),
                            equity: accDetails.equity?.toString(),
                            leverage: accDetails.leverage?.toString(),
                            // Try common SDK property names for margin
                            altMargin: (accDetails.initialMargin || accDetails.posMargin || accDetails.marginUsed || account.margin)?.toString(),
                        }
                    };
                }
            } catch (error) {}
            return null;
        };

        const results = await this.limitParallel(instruments, fetchPortfolio, 8);
        return results.filter(p => p !== null);
    }

    /**
     * Place a market order
     */
    async placeMarketOrder(params: {
        instrumentSymbol: string;
        side: 'LONG' | 'SHORT';
        sizeUsd: number;
        leverage: number;
        slippage?: number;
    }): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const { Side, PERP_EXPIRY } = await import('@synfutures/oyster-sdk');

        const instrument = await this.getInstrument(params.instrumentSymbol);
        if (!instrument) throw new Error(`Instrument ${params.instrumentSymbol} not found`);

        const pair = instrument.getPairModel(PERP_EXPIRY);
        if (!pair) throw new Error(`No perpetual pair found for ${params.instrumentSymbol}`);

        const side = params.side === 'LONG' ? Side.LONG : Side.SHORT;
        const slippage = params.slippage || 100;

        const gateBalances = await this.getGateBalances(signer.address);
        const usdBalance = gateBalances.find((b: any) => ['USDC', 'USDB'].includes(b.symbol.toUpperCase()));
        
        const availableMargin = usdBalance ? parseFloat(ethers.utils.formatUnits(usdBalance.balance, usdBalance.decimals)) : 0;
        let effectiveSizeUsd = params.sizeUsd;
        if (effectiveSizeUsd > availableMargin * 0.9) effectiveSizeUsd = availableMargin * 0.9;
        
        if (effectiveSizeUsd <= 0) throw new Error(`No margin available in Gate. Current: $${availableMargin.toFixed(2)}`);

        await this.sdk.syncVaultCacheWithAllQuotes(signer.address);

        const notionalUsd = effectiveSizeUsd * params.leverage;
        const quoteAmount = ethers.utils.parseEther(notionalUsd.toFixed(18));
        const { baseAmount, quotation } = await this.sdk.inquireByQuote(pair, side, quoteAmount);

        const account = await this.sdk.getPairLevelAccount(signer.address, instrument.info.addr, PERP_EXPIRY);
        
        // Calculate the intended margin in USD based on effectiveSizeUsd and format to 18 decimals
        const intendedMarginUsd = effectiveSizeUsd.toFixed(18);
        const marginToUse = ethers.utils.parseEther(intendedMarginUsd);
        
        const simulation = this.sdk.simulateTrade(account, quotation, side, baseAmount, marginToUse, undefined, slippage);

        // Required minimal margin for the position
        const minReqMargin = simulation.margin || ethers.constants.Zero;
        
        // Ensure we provide at least the minimum required margin, but use our intended margin for correct leverage
        const finalMargin = minReqMargin.gt(marginToUse) ? minReqMargin : marginToUse;
        
        try {
            console.log(`[START] Executing intuitiveTrade...`);
            const tx = await this.sdk.intuitiveTrade(
                signer, pair, side, baseAmount, finalMargin, simulation.tradePrice, slippage, Math.floor(Date.now() / 1000) + 300
            );
            const txHash = tx?.hash || tx?.transactionHash || (typeof tx === 'string' ? tx : undefined);
            return {
                success: true,
                txHash,
                side: params.side,
                size: ethers.utils.formatEther(baseAmount),
                margin: ethers.utils.formatEther(finalMargin),
                leverage: params.leverage
            };
        } catch (orderError: any) {
            const errorMsg = orderError.message || String(orderError);
            if (errorMsg.includes("toString") || errorMsg.includes("handleReceipt")) {
                console.log(`[WARN] SDK bug handleReceipt. Checking position...`);
                await new Promise(resolve => setTimeout(resolve, 3000));
                const updatedAccount = await this.sdk.getPairLevelAccount(signer.address, instrument.info.addr, PERP_EXPIRY);
                if (updatedAccount?.position && !updatedAccount.position.size.isZero()) {
                    return {
                        success: true,
                        txHash: 'confirmed-on-chain',
                        side: params.side,
                        size: ethers.utils.formatEther(baseAmount),
                        margin: ethers.utils.formatEther(finalMargin),
                        warning: 'SDK receipt parsing error'
                    };
                }
            }
            throw orderError;
        }
    }

    /**
     * Close position
     */
    async closePosition(instrumentSymbol: string): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const { Side, PERP_EXPIRY } = await import('@synfutures/oyster-sdk');

        const instrument = await this.getInstrument(instrumentSymbol);
        if (!instrument) throw new Error(`Instrument ${instrumentSymbol} not found`);

        const pair = instrument.getPairModel(PERP_EXPIRY);
        const account = await this.sdk.getPairLevelAccount(signer.address, instrument.info.addr, PERP_EXPIRY);
        const position = account?.position;

        if (!position || position.size.isZero()) return { success: true, message: 'No position' };

        const closeSide = position.side === Side.LONG ? Side.SHORT : Side.LONG;
        const closeSize = position.size.abs();
        
        // Synchronize vault cache to prevent missing quote/margin token references
        await this.sdk.syncVaultCacheWithAllQuotes(signer.address);

        const { quotation } = await this.sdk.inquireByBase(pair, closeSide, closeSize);
        const simulation = this.sdk.simulateTrade(account, quotation, closeSide, closeSize, ethers.constants.Zero, undefined, 200);

        try {
            console.log(`[START] Executing intuitiveTrade to close position...`);
            const tx = await this.sdk.intuitiveTrade(signer, pair, closeSide, closeSize, ethers.constants.Zero, simulation.tradePrice, 200, Math.floor(Date.now() / 1000) + 300);
            return { success: true, txHash: tx?.hash || tx?.transactionHash };
        } catch (error: any) {
            const errorMsg = error.message || String(error);
            if (errorMsg.includes("toString") || errorMsg.includes("handleReceipt")) {
                console.log(`[WARN] SDK bug handleReceipt during close. Checking if position is gone...`);
                await new Promise(resolve => setTimeout(resolve, 3000));
                const updatedAccount = await this.sdk.getPairLevelAccount(signer.address, instrument.info.addr, PERP_EXPIRY);
                if (!updatedAccount?.position || updatedAccount.position.size.isZero()) {
                    console.log(`[OK] Position verified closed on-chain despite SDK receipt error.`);
                    return {
                        success: true,
                        txHash: 'confirmed-on-chain',
                        warning: 'SDK receipt parsing error'
                    };
                }
            }
            console.error(`[ERR] closePosition error:`, error);
            throw error;
        }
    }

    /**
     * Deposit to Gate
     */
    async deposit(tokenSymbol: string, amount: string): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const token = await this.sdk.ctx.getTokenInfo(tokenSymbol);
        const amountParsed = ethers.utils.parseUnits(amount, token.decimals);

        const erc20 = new ethers.Contract(token.address, ['function approve(address,uint256) returns (bool)', 'function allowance(address,address) view returns (uint256)'], signer);
        const allowance = await erc20.allowance(signer.address, this.sdk.contracts.gate.address);
        if (allowance.lt(amountParsed)) await (await erc20.approve(this.sdk.contracts.gate.address, ethers.constants.MaxUint256)).wait();

        const tx = await this.sdk.deposit(signer, token.address, amountParsed);
        return { success: true, txHash: tx.hash };
    }

    /**
     * Withdraw from Gate
     */
    async withdraw(tokenSymbol: string, amount: string): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const token = await this.sdk.ctx.getTokenInfo(tokenSymbol);
        const amountParsed = ethers.utils.parseUnits(amount, token.decimals);
        const tx = await this.sdk.withdraw(signer, token.address, amountParsed);
        return { success: true, txHash: tx.hash };
    }

    /**
     * Add liquidity
     */
    async addLiquidity(params: { instrumentSymbol: string; alphaWad: string; marginAmount: string; slippage?: number }): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const { PERP_EXPIRY } = await import('@synfutures/oyster-sdk');
        const instrument = await this.getInstrument(params.instrumentSymbol);
        const marginWad = ethers.utils.parseUnits(params.marginAmount, 18);

        // Vault cache must be synced before any LP operation (same as closePosition)
        await this.sdk.syncVaultCacheWithAllQuotes(signer.address);

        const instrumentId = { marketType: instrument.marketType, baseSymbol: instrument.info.base.symbol, quoteSymbol: 'USDC' };
        const simulation = await this.sdk.simulateAddLiquidity(signer.address, instrumentId, PERP_EXPIRY, ethers.BigNumber.from(params.alphaWad), marginWad, params.slippage || 100);

        const tx = await this.sdk.addLiquidity(signer, instrumentId, PERP_EXPIRY, simulation.tickDelta, marginWad, simulation.sqrtStrikeLowerPX96, simulation.sqrtStrikeUpperPX96, Math.floor(Date.now() / 1000) + 300);
        return { success: true, txHash: tx?.hash };
    }

    /**
     * Add asymmetric liquidity
     */
    async addAsymmetricLiquidity(params: { instrumentSymbol: string; lowerPercent: number; upperPercent: number; marginAmount: string; slippage?: number }): Promise<any> {
        const avg = (params.lowerPercent + params.upperPercent) / 2;
        const alphaWad = Math.floor((1 + avg) * 1e18).toString();
        return this.addLiquidity({ instrumentSymbol: params.instrumentSymbol, alphaWad, marginAmount: params.marginAmount, slippage: params.slippage });
    }

    /**
     * Remove liquidity
     */
    async removeLiquidity(params: { instrumentSymbol: string; rangeId?: number; slippage?: number }): Promise<any> {
        this.ensureInitialized();
        const signer = this.getSigner();
        const { PERP_EXPIRY } = await import('@synfutures/oyster-sdk');
        const instrument = await this.getInstrument(params.instrumentSymbol);
        const account = await this.sdk.getPairLevelAccount(signer.address, instrument.info.addr, PERP_EXPIRY);
        const range = params.rangeId !== undefined ? account.ranges.find((r: any) => r.id === params.rangeId) : account.ranges[0];
        
        const tx = await this.sdk.removeLiquidity(signer, instrument.info.addr, PERP_EXPIRY, range.tickLower, range.tickUpper, Math.floor(Date.now() / 1000) + 300, params.slippage || 100);
        return { success: true, txHash: tx.hash };
    }

    /**
     * Get LP positions
     */
    async getLPPositions(address: string): Promise<any[]> {
        this.ensureInitialized();
        const { PERP_EXPIRY } = await import('@synfutures/oyster-sdk');
        const instruments = await this.getAllInstruments();
        const fetchLP = async (inst: any) => {
            try {
                const acc = await this.sdk.getPairLevelAccount(address, inst.info.addr, PERP_EXPIRY);
                return acc.ranges?.map((r: any) => ({ instrumentSymbol: inst.info.symbol, liquidity: r.liquidity.toString() })) || null;
            } catch { return null; }
        };
        const res = await this.limitParallel(instruments, fetchLP, 8);
        return res.filter(r => r !== null).flat();
    }

    /**
     * Get signer address
     */
    getSignerAddress(): string | null {
        return this.signer?.address || null;
    }
}
