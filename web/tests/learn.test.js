import test from 'node:test';
import assert from 'node:assert/strict';
import { appendToDraft } from '../modules/learn.js';

test('task template leaves the existing draft intact and separates the new request', () => {
    assert.equal(appendToDraft('My first question  ', 'New task'), 'My first question  \n\nNew task');
    assert.equal(appendToDraft('', 'New task'), 'New task');
    assert.equal(appendToDraft('  ', 'New task'), '  \n\nNew task');
    assert.equal(appendToDraft('```\ncode\n  ', 'New task'), '```\ncode\n  \n\nNew task');
});
