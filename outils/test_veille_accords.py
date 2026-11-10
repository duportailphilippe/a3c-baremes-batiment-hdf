# -*- coding: utf-8 -*-
"""Tests locaux indépendants d'Internet, sans publier de montants fictifs."""
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import veille_accords as veille


class TestVeilleKali(unittest.TestCase):
    def test_selection_des_archives_officielles(self):
        contenu = '''<html><a href="KALI_20261108-121500.tar.gz">d1</a>
        <a href="KALI_20261109-212854.tar.gz">d2</a>
        <a href="Freemium_kali_global_20261108-140000.tar.gz">complet</a></html>'''
        liens = veille.repertoire_officiel(contenu)
        self.assertTrue(liens['quotidien'].endswith('KALI_20261109-212854.tar.gz'))
        self.assertTrue(liens['complet'].endswith('Freemium_kali_global_20261108-140000.tar.gz'))

    def test_archive_sans_nom_reconnu_est_rejetee(self):
        with self.assertRaises(ValueError):
            veille.repertoire_officiel('<a href="archives.zip">archive inconnue</a>')

    def test_filtrage_sectoriel(self):
        btp = 'Hauts-de-France — indemnités de petits déplacements des ouvriers du bâtiment, IDCC 1596'
        tp = 'Hauts-de-France — indemnités de petits déplacements des ouvriers des travaux publics, IDCC 2614'
        autre = 'Bretagne — indemnités de petits déplacements des ouvriers du bâtiment IDCC 1596'
        self.assertTrue(veille.est_pertinent(btp))
        self.assertFalse(veille.est_pertinent(tp))
        self.assertFalse(veille.est_pertinent(autre))

    def test_archive_globale_ignore_les_anciens_accords_identifies(self):
        self.assertTrue(veille.est_accord_deja_connu('Hauts-de-France Accord du 10 juillet 2026 relatif aux indemnités de petits déplacements'))
        self.assertTrue(veille.est_accord_deja_connu('Hauts-de-France Accord du 1er décembre 2025'))
        self.assertFalse(veille.est_accord_deja_connu('Hauts-de-France Accord du 17 novembre 2026 relatif aux indemnités'))
        self.assertFalse(veille.est_accord_deja_connu('Avenant non daté relatif aux indemnités'))

    def test_recuperation_xml_sans_appliquer_le_bareme(self):
        exemple = '''<?xml version="1.0" encoding="utf-8"?>
        <TEXTE_VERSION><ID>KALITEXT000099999999</ID>
        <TITRE_TXT>Hauts-de-France Accord d'exemple sur les indemnités de petits déplacements</TITRE_TXT>
        <CONTENU><p>Les ouvriers du bâtiment IDCC 1596, région Hauts-de-France.</p>
        <p>Indemnités de petits déplacements, zone 1 : transport 3,01 €, trajet 1,68 €.</p>
        </CONTENU></TEXTE_VERSION>'''.encode('utf-8')
        hors_cible = '''<DOCUMENT>Hauts-de-France indemnités de petits déplacements
        des ouvriers des travaux publics IDCC 2614.</DOCUMENT>'''.encode('utf-8')
        with tempfile.TemporaryDirectory() as temp:
            chemin = Path(temp) / 'exemple.tar.gz'
            with tarfile.open(chemin, 'w:gz') as archive:
                for nom, texte in [('textes/KALITEXT000099999999.xml', exemple), ('textes/TP.xml', hors_cible)]:
                    entree = tarfile.TarInfo(nom)
                    entree.size = len(texte)
                    archive.addfile(entree, io.BytesIO(texte))
            candidats, nombre = veille.analyser_archive(str(chemin),'archive locale')
        self.assertEqual(nombre, 2)
        self.assertEqual(len(candidats), 1)
        self.assertIn('legifrance.gouv.fr', candidats[0]['texte_legifrance'])
        self.assertFalse('bareme' in candidats[0])
        self.assertEqual(candidats[0]['indices']['montants_reperes'], ['1,68 €', '3,01 €'])
        ticket = veille.corps_ticket(candidats[0])
        self.assertIn('Aucune mise à jour du barème public', ticket)
        self.assertIn('Arrêté d’extension', ticket)

    def test_ne_cree_pas_de_ticket_deja_present(self):
        candidat = {'cle':'0123456789abcdef','titre':'Accord à examiner','indices':{'dates_reperees':[], 'montants_reperes':[]},
                    'archive_officielle':'archive','chemin_dans_archive':'fic.xml', 'texte_legifrance':None, 'extrait':'extrait'}
        with patch.object(veille, 'tickets_existants', return_value={'0123456789abcdef'}), \
             patch.object(veille,'requete_github') as api:
            nouveaux, ignores = veille.publier_tickets([candidat], 'owner/depot', 'jeton')
            self.assertEqual((nouveaux, ignores),(0,1))
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
