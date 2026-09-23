import request from 'supertest';
import { app } from '../index';

describe('App Integration', () => {
    const originalEnv = process.env;

    beforeEach(() => {
        jest.resetModules();
        process.env = { ...originalEnv, API_KEY: 'test-api-key' };
    });

    afterEach(() => {
        process.env = originalEnv;
    });

    it('allows /health without api key', async () => {
        const res = await request(app).get('/health');
        expect(res.status).toBe(200);
    });

    it('denies access to protected routes without api key', async () => {
        const res = await request(app).get('/instruments'); // Assuming /instruments is protected and exists
        expect(res.status).toBe(401);
    });

    it('allows access to protected routes with valid api key', async () => {
        const res = await request(app)
            .get('/instruments')
            .set('x-api-key', 'test-api-key');
        // It might fail in the service layer, but it should not return 401
        expect(res.status).not.toBe(401);
    });
});
