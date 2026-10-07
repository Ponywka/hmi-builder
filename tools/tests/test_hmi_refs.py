"""Typed reference rewriting: only known forms change, once, and everything else is reported."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hmi_refs as R


def maps(**kw):
    base = dict(page={0: 1, 1: 0, 2: None}, pic={0: 5, 1: 1, 2: None}, font={0: 0, 1: 2},
                comp={0: {0: 0, 1: 2, 2: None, 3: 1}}, page_names_base={0: 'a', 1: 'b', 2: 'c'},
                page_names_final={'a', 'b'}, comp_names_base={0: {0: 'a', 1: 'b0', 2: 't0', 3: 'n0'}},
                comp_names_final={0: {'a', 'b0', 'n0'}}, global_names=set())
    base.update(kw)
    return R.Maps(**base)


def run(text, page=0, strict=False, m=None):
    rw = R.Rewriter(m or maps(), page, strict=strict)
    out = rw.run(text)
    return out, sorted((f.kind, f.domain) for f in rw.findings)


class RewriterTest(unittest.TestCase):
    def test_page_statements(self):
        self.assertEqual(run('page 0'), ('page 1', []))
        self.assertEqual(run('  page 1   //comment'), ('  page 0   //comment', []))
        self.assertEqual(run('page b'), ('page b', []))
        self.assertEqual(run('page dp'), ('page dp', []))
        self.assertEqual(run('page 2')[1], [('dangling', 'page')])
        self.assertEqual(run('page c')[1], [('dangling', 'page')])
        self.assertEqual(run('page cur.val')[1], [('dynamic', 'page')])
        self.assertEqual(run('page 7'), ('page 7', []))              # unknown ids are left alone

    def test_picture_and_font_attributes(self):
        self.assertEqual(run('b0.picc=0')[0], 'b0.picc=5')
        self.assertEqual(run('if(b0.picc==0||b1.pic!=1)')[0], 'if(b0.picc==5||b1.pic!=1)')
        self.assertEqual(run('b0.pic=65535')[0], 'b0.pic=65535')
        self.assertEqual(run('t0.font=1')[0], 't0.font=2')
        self.assertEqual(run('b0.pic=2')[1], [('dangling', 'pic')])
        self.assertEqual(run('b0.pic=v.val')[1], [('dynamic', 'pic')])
        self.assertEqual(run('b0.pic=1+x')[1], [('dynamic', 'pic')])
        self.assertEqual(run('b0.pic>3')[1], [('ordered', 'pic')])
        self.assertEqual(run('b0.pic+=1'), ('b0.pic+=1', [('ordered', 'pic')]))

    def test_drawing_commands(self):
        self.assertEqual(run('pic 10,10,0')[0], 'pic 10,10,5')
        self.assertEqual(run('picq 1,2,3,4,0')[0], 'picq 1,2,3,4,5')
        self.assertEqual(run('xpic 1,2,3,4,5,6,0')[0], 'xpic 1,2,3,4,5,6,5')
        self.assertEqual(run('xstr 0,0,10,10,1,1,2,3,4,1,"page 0 pic 1"')[0], 'xstr 0,0,10,10,2,1,2,3,4,1,"page 0 pic 1"')
        self.assertEqual(run('pic 1,2,n.val')[1], [('dynamic', 'pic')])

    def test_components(self):
        self.assertEqual(run('b[1].pic=0')[0], 'b[2].pic=5')
        self.assertEqual(run('b[3].txt="x"')[0], 'b[1].txt="x"')
        self.assertEqual(run('b[0].pic=65535')[0], 'b[0].pic=65535')
        self.assertEqual(run('p[0].b[1].txt="x"')[0], 'p[1].b[2].txt="x"')
        self.assertEqual(run('vis 1,1')[0], 'vis 2,1')
        self.assertEqual(run('tsw 255,0')[0], 'tsw 255,0')
        self.assertEqual(run('click 3,1')[0], 'click 1,1')
        self.assertEqual(run('b[2].pic=0')[1], [('dangling', 'comp')])
        self.assertEqual(run('b[x.val].pic=0')[1], [('dynamic', 'comp')])
        self.assertEqual(run('p[v.val].b[1].txt="x"')[1], [('dynamic', 'comp'), ('dynamic', 'page')])
        self.assertEqual(run('p[dp].b[1].txt="x"')[1], [('dynamic', 'comp')])
        self.assertEqual(run('b[1].pic=0', page=None)[1], [('dynamic', 'comp')])

    def test_names(self):
        self.assertEqual(run('t0.txt="x"')[1], [('dangling', 'comp')])
        self.assertEqual(run('b0.txt="x"')[1], [])
        self.assertEqual(run('c.b0.txt="x"')[1], [('dangling', 'page')])
        self.assertEqual(run('a.t0.txt="x"')[1], [('dangling', 'comp')])
        self.assertEqual(run('t0.txt="x"', m=maps(global_names={'t0'}))[1], [])

    def test_comments_strings_and_placeholders_are_not_touched(self):
        for text in ('t="page 0 b0.pic=0" //page 0', '// pic 1,2,0', 'x.txt="${page:k}"'):
            self.assertEqual(run(text, m=maps(comp_names_final={0: {'a', 'b0', 't0'}}))[0], text, text)
        out, _ = run('page ${page:k}')
        self.assertEqual(out, 'page ${page:k}')
        out, found = run('b0.pic=${picture:k}')
        self.assertEqual((out, found), ('b0.pic=${picture:k}', []))
        self.assertEqual(R.expand('page ${page:k} b0.pic=${picture:p}', lambda k, v: {'page': 9, 'picture': 4}[k]),
                         'page 9 b0.pic=4')

    def test_escaped_quotes_do_not_end_a_string(self):
        for text in ('b1.txt="say \\"b[3]\\" page 0 b0.pic=0"', 't="a\\\\" page 0'):
            out, found = run(text, m=maps(comp_names_final={0: {'a', 'b0', 't0', 'n0'}}))
            self.assertEqual(out.split('" page')[0], text.split('" page')[0], text)
        self.assertEqual(run('b1.txt="x\\"" \nb0.pic=0')[0], 'b1.txt="x\\"" \nb0.pic=5')

    def test_reversed_comparisons(self):
        self.assertEqual(run('if(0==b1.pic)')[0], 'if(5==b1.pic)')
        self.assertEqual(run('if(1!=b1.picc)')[0], 'if(1!=b1.picc)')
        self.assertEqual(run('if(1==t0.font)')[0], 'if(2==t0.font)')
        self.assertEqual(run('if(3<b1.pic)')[1], [('ordered', 'pic')])

    def test_closing_brace_after_page(self):
        self.assertEqual(run('if(x.val==1){page 1}'), ('if(x.val==1){page 0}', []))

    def test_placeholder_pages_with_literal_components(self):
        text = 'p[${page:k}].b[1].txt="x"'
        self.assertEqual(run(text, strict=True)[1], [('newref', 'comp')])
        self.assertEqual(run(text)[1], [('dynamic', 'comp')])

    def test_page_copies_use_their_own_component_map_and_names(self):
        own = {0: 0, 1: 1, 2: 2, 3: 3}                      # this copy does not move anything
        rw = R.Rewriter(maps(), 0, own_comp=own, own_names={'a', 'b0', 't0', 'n0'})
        self.assertEqual(rw.run('b[x.val].pic=65535 \nt0.txt="x"'), 'b[x.val].pic=65535 \nt0.txt="x"')
        self.assertEqual(rw.findings, [])
        moved = R.Rewriter(maps(), 0, own_comp={0: 0, 1: 2, 2: 1, 3: 3}, own_names={'a'})
        moved.run('b[x.val].pic=65535')
        self.assertEqual([f.kind for f in moved.findings], ['dynamic'])

    def test_external_observers_include_get_and_print(self):
        for text in ('get dp', 'print dp', 'prints dp,1', 'printh 1,dp'):
            self.assertEqual(run(text)[1], [('external', 'page')], text)

    def test_each_line_is_remapped_once(self):
        swap = maps(pic={0: 1, 1: 0}, page={0: 1, 1: 0})
        self.assertEqual(run('b0.pic=0\nb1.pic=1\npage 0\npage 1', m=swap)[0], 'b0.pic=1\nb1.pic=0\npage 1\npage 0')

    def test_strict_new_code_rejects_numbers_when_ids_move(self):
        self.assertEqual(run('b0.pic=0', strict=True)[1], [('newref', 'pic')])
        self.assertEqual(run('page 0', strict=True)[1], [('newref', 'page')])
        same = maps(page={0: 0, 1: 1}, pic={0: 0, 1: 1}, font={0: 0}, comp={0: {0: 0, 1: 1}},
                    page_names_final={'a', 'b', 'c'}, comp_names_final={0: {'a', 'b0', 't0', 'n0'}})
        self.assertEqual(run('b0.pic=0\npage 1', strict=True, m=same), ('b0.pic=0\npage 1', []))

    def test_external_observers(self):
        self.assertEqual(run('prints dp,1')[1], [('external', 'page')])
        self.assertEqual(run('sendme')[1], [('external', 'page')])
        self.assertEqual(run('prints 0,1')[1], [])
        self.assertEqual(run('prints 0x65,1')[0], 'prints 0x65,1')

    def test_dynamic_findings_need_a_moving_id(self):
        same = maps(page={0: 0, 1: 1, 2: None}, page_names_final={'a', 'b'}, pic={0: 0}, comp={0: {0: 0, 1: 1}})
        self.assertEqual(run('page cur.val', m=same)[1], [])                 # deleting the tail moves no survivor
        self.assertEqual(run('b0.pic=v.val', m=same)[1], [])

    def test_attribute_domains(self):
        self.assertEqual(R.attr_domain(98, 'picc'), ('pic', 65535))
        self.assertEqual(R.attr_domain(121, 'pic'), ('pic', 65535))
        self.assertEqual(R.attr_domain(98, 'font'), ('font', None))
        self.assertEqual(R.attr_domain(121, 'left'), ('page', 255))
        self.assertEqual(R.attr_domain(2, 'vid'), ('anim', 65535))
        for t, n in ((98, 'left'), (116, 'key'), (69, 'vid'), (98, 'val'), (121, 'font'), (98, 'groupid0')):
            self.assertIsNone(R.attr_domain(t, n), (t, n))


if __name__ == '__main__':
    unittest.main()
