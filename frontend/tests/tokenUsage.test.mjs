import assert from 'node:assert/strict';
import {test} from 'node:test';
import {summarizeAgentUsage, summarizeJudgeUsage} from '../src/tokenUsage.ts';

const request = (id, operation_key, usage, extra = {}) => ({id, operation_key, usage, evaluation_id: null, ...extra});

test('sums agent tool follow-up, recovery and reported retries without Judge usage', () => {
 const result = summarizeAgentUsage([
  request('first', 'online:agent:v0:step0', {input_tokens: 100, output_tokens: 10, total_tokens: 110}),
  request('tool', 'online:agent:v0:step1', {input_tokens: 200, output_tokens: 20, total_tokens: 220}),
  request('retry', 'online:agent:v0:step1', {input_tokens: 200, output_tokens: 5, total_tokens: 205}),
  request('recovery', 'online:agent:v1:step0', {input_tokens: 50, output_tokens: 15}),
  request('judge', 'online:judge:e1:validation0', {input_tokens: 900, output_tokens: 90}, {evaluation_id: 'e1'}),
  request('offline', 'offline:judge:e2:validation0', {input_tokens: 800, output_tokens: 80}, {evaluation_id: 'e2'}),
 ]);
 assert.deepEqual(result, {input: 550, output: 50, total: 600, reported: 4, requests: 4});
});

test('distinguishes unreported usage from mock zero tokens', () => {
 assert.deepEqual(summarizeAgentUsage(), {input: null, output: null, total: null, reported: 0, requests: 0});
 assert.deepEqual(summarizeAgentUsage([request('pending', 'online:agent:v0:step0', null)]),
  {input: null, output: null, total: null, reported: 0, requests: 1});
 assert.deepEqual(summarizeAgentUsage([request('mock', 'online:agent:v0:step0', {input_tokens: 0, output_tokens: 0})]),
  {input: 0, output: 0, total: 0, reported: 1, requests: 1});
});

test('keeps known subtotals when other requests have no usage', () => {
 assert.deepEqual(summarizeAgentUsage([
  request('first', 'online:agent:v0:step0', {input_tokens: 100, output_tokens: 25}),
  request('failed', 'online:agent:v0:step1', null),
 ]), {input: 100, output: 25, total: 125, reported: 1, requests: 2});
});

test('reads stored response usage and does not double-count cached or reasoning tokens', () => {
 const usage = {input_tokens: 100, output_tokens: 30, total_tokens: 130,
  input_tokens_details: {cached_tokens: 40}, output_tokens_details: {reasoning_tokens: 20}};
 assert.deepEqual(summarizeAgentUsage([
  request('stored', 'online:agent:v0:step0', null, {response_payload: {usage}}),
 ]), {input: 100, output: 30, total: 130, reported: 1, requests: 1});
});

test('invalid counts remain unknown instead of being coerced or rendered as NaN', () => {
 assert.deepEqual(summarizeAgentUsage([
  request('invalid', 'online:agent:v0:step0', {input_tokens: -1, output_tokens: '20', total_tokens: NaN}),
 ]), {input: null, output: null, total: null, reported: 0, requests: 1});
});

test('sums Judge initial, validation retry, transport retry and recovery evaluations', () => {
 const requests = [
  request('agent', 'online:agent:v0:step0', {input_tokens: 1000, output_tokens: 100}),
  request('initial', 'online:judge:e1:validation0', {input_tokens: 100, output_tokens: 20}, {evaluation_id: 'e1'}),
  request('validation', 'online:judge:e1:validation1', {input_tokens: 110, output_tokens: 30}, {evaluation_id: 'e1'}),
  request('transport', 'online:judge:e1:validation1', {input_tokens: 90, output_tokens: 10}, {evaluation_id: 'e1'}),
  request('recovery', 'online:judge:e2:validation0', {input_tokens: 50, output_tokens: 5}, {evaluation_id: 'e2'}),
 ];
 assert.deepEqual(summarizeJudgeUsage(requests), {input: 350, output: 65, total: 415, reported: 4, requests: 4});
 assert.deepEqual(summarizeAgentUsage(requests), {input: 1000, output: 100, total: 1100, reported: 1, requests: 1});
});

test('separates online and offline Judge usage for the selected tab', () => {
 const requests = [
  request('online', 'online:judge:e1:validation0', {input_tokens: 100, output_tokens: 10}, {evaluation_id: 'e1'}),
  request('offline', 'offline:judge:e2:validation0', {input_tokens: 500, output_tokens: 40}, {evaluation_id: 'e2'}),
 ];
 assert.deepEqual(summarizeJudgeUsage(requests, 'online'), {input: 100, output: 10, total: 110, reported: 1, requests: 1});
 assert.deepEqual(summarizeJudgeUsage(requests, 'offline'), {input: 500, output: 40, total: 540, reported: 1, requests: 1});
});

test('Judge usage preserves mock zero and partial or missing provider reports', () => {
 assert.deepEqual(summarizeJudgeUsage(), {input: null, output: null, total: null, reported: 0, requests: 0});
 const mock = request('mock', 'online:judge:e1:validation0', {input_tokens: 0, output_tokens: 0}, {evaluation_id: 'e1'});
 assert.deepEqual(summarizeJudgeUsage([mock]), {input: 0, output: 0, total: 0, reported: 1, requests: 1});
 const failed = request('failed', 'online:judge:e2:validation0', null, {evaluation_id: 'e2'});
 assert.deepEqual(summarizeJudgeUsage([failed]), {input: null, output: null, total: null, reported: 0, requests: 1});
 const recorded = request('recorded', 'online:judge:e2:validation0', null,
  {evaluation_id: 'e2', response_payload: {usage: {input_tokens: 120, output_tokens: 30}}});
 assert.deepEqual(summarizeJudgeUsage([failed, recorded]), {input: 120, output: 30, total: 150, reported: 1, requests: 2});
});
